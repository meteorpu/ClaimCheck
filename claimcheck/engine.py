"""Rule engine + claim ledger.

The engine is deterministic: given the same claims, rules and records it
always gives the same answer. The LLM is used only to explain results
(see explain.py), never to decide them.

Claims are not independent (authorization drawdown, duplicates, daily
maximums), so every check reads a shared Ledger, and claims must be
processed in submission order.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

# Which code a payer reports when a claim breaks several rules.
# Learned from claims_history.csv (see backtest.py --precedence): payers check
# evidence of care (EVV) first and timely filing last.
DEFAULT_PRECEDENCE = ["D01", "D10", "D09", "D08", "D07", "D04", "D05", "D06", "D02", "D03"]

FIX_DATA = "data fix"          # the right value exists in our records: can be auto-proposed
FIX_PROCESS = "process fix"    # someone outside billing must act first
FIX_NONE = "not fixable"


@dataclass
class Violation:
    code: str
    reason: str
    source: str
    fix_type: str
    fix: str
    patch: dict = field(default_factory=dict)  # proposed field changes (data fixes only)


@dataclass
class Ledger:
    """Shared state across claims: what has been submitted and paid so far."""
    auth_used: dict = field(default_factory=lambda: defaultdict(int))        # auth_id -> units paid
    submitted: dict = field(default_factory=dict)                            # (patient, svc, dos, payer) -> claim_id
    submitted_status: dict = field(default_factory=dict)                     # claim_id -> status
    daily_paid: dict = field(default_factory=lambda: defaultdict(int))      # (patient, payer, dos) -> units paid

    def record(self, claim: dict, status: str) -> None:
        key = (claim["patient_id"], claim["service_code"], claim["dos"], claim["payer_id"])
        self.submitted.setdefault(key, claim["claim_id"])
        self.submitted_status[claim["claim_id"]] = status
        if status == "paid":
            if claim["auth_id"]:
                self.auth_used[claim["auth_id"]] += claim["units"]
            self.daily_paid[(claim["patient_id"], claim["payer_id"], claim["dos"])] += claim["units"]


def _in_range(d: pd.Timestamp, start, end) -> bool:
    if start is not None and d < pd.Timestamp(start):
        return False
    if end is not None and d > pd.Timestamp(end):
        return False
    return True


def units_from_minutes(minutes: float, rounding: str) -> int:
    full, rest = divmod(int(round(minutes)), 15)
    if rounding == "eight_minute_rule" and rest >= 8:
        full += 1
    return full


class Engine:
    def __init__(self, rules: dict, evv: pd.DataFrame, auths: pd.DataFrame, precedence=None):
        self.rules = rules
        self.precedence = precedence or DEFAULT_PRECEDENCE
        self.auths = auths.set_index("auth_id")
        self.auths_df = auths
        # index EVV by (patient, service, date)
        self.evv = {k: g for k, g in evv.groupby(["patient_id", "service_code", "date"])}

    # ---------- individual checks ----------
    def _check_evv(self, c, pr, out):
        visits = self.evv.get((c["patient_id"], c["service_code"], c["dos"]))
        verified = visits[visits.verification_status == "verified"] if visits is not None else None
        if verified is None or verified.empty:
            if visits is not None and not visits.empty:
                v = visits.iloc[0]
                reason = f"EVV visit {v.visit_id} exists but is '{v.verification_status}', so it does not count as proof of care"
                fix = (f"Ask the care coordinator to resolve visit {v.visit_id} with the EVV aggregator, then re-check"
                       if v.verification_status == "manual_edit_pending"
                       else f"Visit {v.visit_id} was rejected: coordinator to investigate before billing")
            else:
                reason = "No EVV visit recorded for this patient, service and date"
                fix = "Do not submit: confirm with the coordinator whether the visit happened and was logged"
            out.append(Violation("D01", reason, "Payer manual: EVV requirement", FIX_PROCESS, fix))
            return
        minutes = verified.minutes.sum()
        allowed = units_from_minutes(minutes, pr["rounding"])
        if c["units"] > allowed:
            out.append(Violation(
                "D09",
                f"Billed {c['units']} units but verified EVV time is {int(minutes)} min = {allowed} units "
                f"under {pr['name']}'s rounding rule ({pr['rounding'].replace('_', ' ')})",
                pr["rounding_source"], FIX_DATA,
                f"Reduce units to {allowed} (what EVV supports)", {"units": allowed}))

    def _find_auth(self, c):
        a = self.auths_df
        m = a[(a.patient_id == c["patient_id"]) & (a.service_code == c["service_code"]) &
              (a.payer_id == c["payer_id"]) & (a.start_date <= c["dos"]) & (a.end_date >= c["dos"])]
        return None if m.empty else m.iloc[0]

    def _check_auth(self, c, pr, ledger, out):
        if not c["auth_id"]:
            match = self._find_auth(c)
            if match is not None:
                out.append(Violation("D07", "No prior authorization number on the claim", "Payer manual: prior authorization",
                                     FIX_DATA, f"Add authorization {match.auth_id} (covers this patient, service and date)",
                                     {"auth_id": match.auth_id}))
            else:
                out.append(Violation("D07", "No prior authorization number, and no valid authorization found on file",
                                     "Payer manual: prior authorization", FIX_PROCESS,
                                     "Request an authorization from the payer before billing"))
            return
        if c["auth_id"] not in self.auths.index:
            out.append(Violation("D08", f"Authorization {c['auth_id']} not found", "Payer manual: prior authorization",
                                 FIX_PROCESS, "Check the authorization number with the payer"))
            return
        a = self.auths.loc[c["auth_id"]]
        problems = []
        if a.patient_id != c["patient_id"]:
            problems.append(f"it is for patient {a.patient_id}")
        if a.service_code != c["service_code"]:
            problems.append(f"it is for service {a.service_code}")
        if a.payer_id != c["payer_id"]:
            problems.append(f"it is from payer {a.payer_id}")
        if not (a.start_date <= c["dos"] <= a.end_date):
            problems.append(f"it covers {a.start_date:%d %b} to {a.end_date:%d %b}, not {c['dos']:%d %b %Y}")
        if problems:
            match = self._find_auth(c)
            if match is not None:
                out.append(Violation("D08", f"Authorization {c['auth_id']} does not fit: " + "; ".join(problems),
                                     "Payer manual: prior authorization", FIX_DATA,
                                     f"Use authorization {match.auth_id} instead", {"auth_id": match.auth_id}))
            else:
                out.append(Violation("D08", f"Authorization {c['auth_id']} does not fit: " + "; ".join(problems),
                                     "Payer manual: prior authorization", FIX_PROCESS,
                                     "No valid authorization on file: request one from the payer"))
            return
        remaining = int(a.units_authorized) - ledger.auth_used[c["auth_id"]]
        if c["units"] > remaining:
            out.append(Violation("D02", f"Authorization {c['auth_id']} has {max(remaining, 0)} units left; claim bills {c['units']}",
                                 "Payer manual: authorization drawdown", FIX_PROCESS,
                                 "Request more units from the payer (scheduler: authorization is used up)"))

    def _check_timely(self, c, pr, out):
        age = (c["submitted"] - c["dos"]).days
        for rule in pr["timely_filing"]:
            if _in_range(c["dos"], rule["dos_from"], rule["dos_to"]) and age > rule["days"]:
                out.append(Violation("D03", f"Submitted {age} days after the date of service; limit is {rule['days']} days",
                                     rule["source"], FIX_NONE, "Past the filing limit: write off or appeal with good cause"))

    def _check_duplicate(self, c, ledger, out):
        if c["frequency_code"] == "7":
            return  # corrected claims are processed in their own right
        key = (c["patient_id"], c["service_code"], c["dos"], c["payer_id"])
        prior = ledger.submitted.get(key)
        if prior and prior != c["claim_id"]:
            status = ledger.submitted_status.get(prior)
            if status == "denied":
                out.append(Violation("D05", f"Same patient, service, date and payer as claim {prior}, which was denied",
                                     "billing_basics: duplicates", FIX_DATA,
                                     f"Send as a corrected claim: frequency code 7 with original_claim_id {prior}",
                                     {"frequency_code": "7", "original_claim_id": prior}))
            else:
                out.append(Violation("D05", f"Duplicate of claim {prior} ({status or 'already submitted'})",
                                     "billing_basics: duplicates", FIX_NONE, "Do not submit: this visit is already billed"))

    def _check_daily_max(self, c, pr, ledger, out):
        dm = pr.get("daily_max_units")
        if not dm:
            return
        so_far = ledger.daily_paid[(c["patient_id"], c["payer_id"], c["dos"])]
        if so_far + c["units"] > dm["units"]:
            out.append(Violation("D06", f"Patient already has {so_far} paid units that day; this claim would exceed the {dm['units']}-unit daily maximum",
                                 dm["source"], FIX_PROCESS,
                                 f"Bill at most {max(dm['units'] - so_far, 0)} units, and review the care schedule"))

    def _check_modifier(self, c, pr, out):
        for rule in pr["modifiers"]:
            if rule["service"] != c["service_code"] or not _in_range(c["dos"], rule["dos_from"], rule["dos_to"]):
                continue
            weekend = c["dos"].weekday() >= 5
            if rule.get("weekdays_only") and weekend:
                continue
            if rule.get("weekends_only") and not weekend:
                continue
            if c["modifier"] != rule["required"]:
                day = f" ({c['dos']:%A})" if rule.get("weekdays_only") or rule.get("weekends_only") else ""
                out.append(Violation("D04", f"{c['service_code']} on {c['dos']:%d %b}{day} needs modifier {rule['required']}, claim has {c['modifier'] or 'none'}",
                                     rule["source"], FIX_DATA, f"Set modifier to {rule['required']}", {"modifier": rule["required"]}))

    def _check_contract(self, c, pr, out):
        for rule in pr["contract_rules"]:
            if not _in_range(c["dos"], rule["dos_from"], rule["dos_to"]):
                continue
            k = rule.get("units_multiple_of")
            if k and c["units"] % k:
                fixed = c["units"] - c["units"] % k
                out.append(Violation(rule["denial_code"], f"{rule['description']}; claim bills {c['units']} units",
                                     rule["source"], FIX_DATA if fixed > 0 else FIX_NONE,
                                     f"Bill {fixed} units (whole hours)" if fixed > 0 else "Less than one hour: not billable",
                                     {"units": fixed} if fixed > 0 else {}))

    # ---------- public API ----------
    def violations(self, c: dict, ledger: Ledger) -> list[Violation]:
        pr = self.rules["payers"].get(c["payer_id"])
        if pr is None:
            return [Violation("D99", "Unknown payer", "", FIX_PROCESS, "Fix the payer name")]
        out: list[Violation] = []
        self._check_duplicate(c, ledger, out)
        self._check_timely(c, pr, out)
        self._check_auth(c, pr, ledger, out)
        self._check_evv(c, pr, out)
        self._check_modifier(c, pr, out)
        self._check_contract(c, pr, out)
        self._check_daily_max(c, pr, ledger, out)
        return out

    def propose_fix(self, c: dict, ledger: Ledger, max_rounds: int = 4) -> tuple[dict, list[str], list[Violation]]:
        """Apply data fixes one at a time and re-check, until the claim is clean or
        only non-data problems remain. Returns (combined patch, fix steps, remaining problems).
        Every value comes from an existing record; the ledger is only read."""
        patch: dict = {}
        steps: list[str] = []
        current = dict(c)
        for _ in range(max_rounds):
            v = self.predict(current, ledger)[2]
            data = [x for x in v if x.fix_type == FIX_DATA and x.patch]
            if not v or not data or len(data) < len(v):
                return patch, steps, v
            patch.update(data[0].patch)
            steps.append(data[0].fix)
            current.update(data[0].patch)
        return patch, steps, self.predict(current, ledger)[2]

    def predict(self, c: dict, ledger: Ledger) -> tuple[str, str | None, list[Violation]]:
        v = self.violations(c, ledger)
        if not v:
            return "paid", None, v
        v.sort(key=lambda x: self.precedence.index(x.code) if x.code in self.precedence else 99)
        return "denied", v[0].code, v


def order(df: pd.DataFrame) -> pd.DataFrame:
    """Payers draw down authorizations in the order claims are submitted."""
    return df.sort_values(["submitted", "claim_id"], kind="stable")


def records(df: pd.DataFrame) -> list[dict]:
    """Rows as dicts with real None for blanks (pandas would give NaN, which is truthy)."""
    return [{k: (None if not isinstance(v, (list, dict)) and pd.isna(v) else v) for k, v in r.items()}
            for r in order(df).to_dict("records")]
