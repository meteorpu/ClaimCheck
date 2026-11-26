"""The Monitor: a routine scan that looks for things billing staff would not notice on their own.

It runs on a schedule (see monitor_scan.py) or on demand from the app, and produces a report:
  - a short summary of the last scan
  - findings, each phrased as a suggestion a person can accept, dismiss or snooze

What it looks for (all from data we already hold; nothing is changed automatically):
  1. New rejection reasons: a payer starts rejecting for a reason it hardly used before.
     That is usually a rule change (a bulletin, a contract amendment) and sometimes one nobody announced.
  2. Spikes: a rejection reason that suddenly happens much more often than usual.
  3. Payer documents changed: a manual or bulletin differs from the version the rule table was approved against.
  4. Authorizations running out early: at the current pace, a patient's approved hours run out before the end date.
  5. This batch: the most common avoidable problems in the claims waiting to be sent.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pandas as pd

from .engine import FIX_DATA
from .load import DATA_DIR, ROOT

REPORT_PATH = ROOT / "output" / "monitor_report.json"
BOARD_PATH = ROOT / "output" / "suggestion_board.json"
SNAPSHOT_PATH = ROOT / "output" / "document_snapshot.json"

PAYER_NAME = {"PY01": "Northwind", "PY02": "Harbor", "PY03": "Summit"}

# Plain-English names for the payers' rejection codes.
REASON = {
    "D01": "no verified visit record",
    "D02": "not enough authorized hours left",
    "D03": "sent after the deadline",
    "D04": "wrong or missing billing tag (modifier)",
    "D05": "sent twice (duplicate)",
    "D06": "too many hours billed for one day",
    "D07": "authorization number missing",
    "D08": "authorization number doesn't match the visit",
    "D09": "billed more time than the visit record shows",
    "D10": "breaks a payer contract rule",
    "D99": "held by the payer for manual review",
}

# What usually causes each reason, and who can prevent it. Used to phrase suggestions.
CAUSE = {
    "D01": ("visits not confirmed in the visit-tracking system before billing", "care coordinators"),
    "D02": ("patients using up their approved hours faster than planned", "schedulers"),
    "D03": ("claims waiting too long before being sent", "billing team"),
    "D04": ("billing tags not updated after a payer changed its requirements", "billing team"),
    "D05": ("the same visit being sent twice instead of as a correction", "billing team"),
    "D06": ("several services on one day adding up to more than the payer allows", "schedulers"),
    "D07": ("authorization numbers left off claims", "billing team"),
    "D08": ("old or wrong authorization numbers copied onto claims", "billing team / authorization team"),
    "D09": ("hours billed from the schedule instead of from the visit record", "billing team"),
    "D10": ("a payer contract rule", "billing manager"),
    "D99": ("payer-side reviews we cannot see into", "billing manager"),
}


def payer_name(pid: str) -> str:
    return PAYER_NAME.get(pid, pid)


def _month_label(p) -> str:
    return pd.Period(p, "M").strftime("%B %Y")


# ---------------------------------------------------------------------------
# 1 + 2. Rejection patterns over time
# ---------------------------------------------------------------------------
def rejection_counts(history: pd.DataFrame) -> pd.DataFrame:
    """Rows: (payer, reason code). Columns: month. Values: rejected claims."""
    h = history[history.status == "denied"].copy()
    h["month"] = h.dos.dt.to_period("M")
    return h.pivot_table(index=["payer_id", "denial_code"], columns="month", values="claim_id",
                         aggfunc="count").fillna(0).astype(int)


GROUP_CODE = {"timely_filing": "D03", "modifiers": "D04"}


def _rules_for(rules: dict, payer: str, code: str, month: pd.Period) -> list[str]:
    """Rule-list entries for this payer and rejection reason that start in this month (so the change is known)."""
    p = rules["payers"][payer]
    hits = []
    for group in ("timely_filing", "modifiers", "contract_rules"):
        for r in p.get(group) or []:
            start = r.get("dos_from")
            if (r.get("denial_code") or GROUP_CODE.get(group)) == code and start and pd.Timestamp(start).to_period("M") == month:
                hits.append(r.get("source", ""))
    return hits


def pattern_findings(history: pd.DataFrame, rules: dict) -> list[dict]:
    counts = rejection_counts(history)
    months = list(counts.columns)
    out = []
    for (payer, code), row in counts.iterrows():
        for i in range(1, len(months)):
            m, now = months[i], int(row.iloc[i])
            before = row.iloc[:i]
            usual = before.mean()
            known = _rules_for(rules, payer, code, m)
            if before.max() == 0 and now >= 5:
                kind = "new_reason"
            elif i >= 2 and now >= 8 and now >= 2 * usual + 3:
                kind = "spike"
            else:
                continue
            inferred = any("inferred" in s for s in known)
            pn, reason = payer_name(payer), REASON.get(code, code)
            cause, team = CAUSE.get(code, ("", "billing team"))
            if kind == "new_reason":
                title = f"{pn} started rejecting claims for a new reason in {_month_label(m)}"
                what = (f"{pn} rejected {now} claims for \"{reason}\" in {_month_label(m)}. "
                        f"Before that it had not used this reason at all.")
                why = ("A new rejection reason usually means the payer changed a rule. "
                       + ("We found no announcement in the payer's documents, so this rule was worked out from the "
                          "rejection notes and added to the rule list as unconfirmed."
                          if inferred else
                          "It matches a change already in our rule list." if known else
                          "It is not explained by any rule in our list yet."))
                action = (f"Confirm the rule with {pn} (ask for the contract change in writing)."
                          if inferred or not known else "No action needed: the rule list already covers it.")
                severity = "high" if (inferred or not known) else "info"
            else:
                title = f"{pn}: more claims rejected for \"{reason}\" in {_month_label(m)}"
                what = (f"{now} {pn} claims were rejected for \"{reason}\" in {_month_label(m)}, "
                        f"against about {usual:.0f} a month before.")
                why = (f"This is usually caused by {cause}." if cause else "")
                if known:
                    why += " A rule change started that month: " + "; ".join(known) + "."
                action = f"Ask the {team} to look at what changed in {_month_label(m)}."
                severity = "medium"
            out.append(dict(
                id=f"{kind}:{payer}:{code}:{m}", kind=kind, severity=severity, title=title,
                what=what, why=why, action=action, team=team, payer=payer, code=code,
                evidence={str(k): int(v) for k, v in row.items()},
                month=str(m),
            ))
            break  # one finding per payer + reason: the first time it happened
    return out


# ---------------------------------------------------------------------------
# 3. Payer document watch
# ---------------------------------------------------------------------------
def _doc_hashes() -> dict[str, str]:
    files = [DATA_DIR / "billing_basics.md", DATA_DIR / "denial_codes.md", *sorted((DATA_DIR / "payers").glob("*.md"))]
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()[:16] for f in files if f.exists()}


def document_findings(rules: dict) -> tuple[list[dict], dict]:
    """Compare payer documents with the snapshot taken when the rule list was approved.
    The first scan takes the snapshot. A changed or new document becomes a 'review this' suggestion."""
    now = _doc_hashes()
    if not SNAPSHOT_PATH.exists():
        SNAPSHOT_PATH.parent.mkdir(exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps({"rule_version": rules["version"], "documents": now}, indent=1))
    snap = json.loads(SNAPSHOT_PATH.read_text())["documents"]
    out = []
    for name, h in now.items():
        if snap.get(name) != h:
            new = name not in snap
            out.append(dict(
                id=f"doc:{name}:{h}", kind="document", severity="high",
                title=f"Payer document {'added' if new else 'changed'}: {name}",
                what=f"{name} is {'new' if new else 'different from the version our rule list was checked against'}.",
                why="Payers announce rule changes in manuals and bulletins. If a rule changed, claims could start "
                    "being rejected before anyone notices.",
                action="Read the change and update the rule list (Claude can draft the update for a person to approve).",
                team="billing manager", payer=None, code=None, evidence={}, month=None))
    status = {"checked": len(now), "changed": len(out)}
    return out, status


def accept_documents(rules: dict) -> None:
    """Called when a person has reviewed document changes: the current versions become the baseline."""
    SNAPSHOT_PATH.write_text(json.dumps({"rule_version": rules["version"], "documents": _doc_hashes()}, indent=1))


# ---------------------------------------------------------------------------
# 4. Authorizations running out early
# ---------------------------------------------------------------------------
def auth_burn(engine, history: pd.DataFrame, batch_res: pd.DataFrame, as_of: pd.Timestamp, margin_days: int = 7) -> pd.DataFrame:
    paid = pd.concat([history[history.status == "paid"], batch_res[batch_res.pred_status == "paid"]])
    used = paid[paid.dos <= as_of].groupby("auth_id").units.sum()
    a = engine.auths_df
    a = a[(a.start_date <= as_of) & (a.end_date > as_of)].copy()
    a["used"] = a.auth_id.map(used).fillna(0).astype(int)
    a = a[a.used > 0]
    days_elapsed = (as_of - a.start_date).dt.days + 1
    a["pct_used"] = a.used / a.units_authorized
    a["pct_time"] = days_elapsed / ((a.end_date - a.start_date).dt.days + 1)
    a["runs_out"] = a.start_date + pd.to_timedelta(a.units_authorized / (a.used / days_elapsed), unit="D")
    return a[a.runs_out < a.end_date - pd.Timedelta(days=margin_days)].sort_values("runs_out")


def auth_finding(alerts: pd.DataFrame, as_of: pd.Timestamp) -> list[dict]:
    if alerts.empty:
        return []
    soon = alerts[alerts.runs_out <= as_of + pd.Timedelta(days=14)]
    return [dict(
        id=f"auth:{as_of.date()}:{len(alerts)}", kind="authorization", severity="high" if len(soon) else "medium",
        title=f"{len(alerts)} patients' approved hours will run out early",
        what=(f"At the current pace, {len(alerts)} authorizations will be used up at least a week before they end"
              f" ({len(soon)} within the next two weeks)."),
        why="Visits after the hours run out are rejected (\"not enough authorized hours left\"). "
            "Schedulers can ask the payer for more hours or adjust the schedule in time.",
        action="Send the list to the schedulers.",
        team="schedulers", payer=None, code="D02", evidence={"alerts": len(alerts), "within_14_days": len(soon)},
        month=None)]


# ---------------------------------------------------------------------------
# 5. This batch
# ---------------------------------------------------------------------------
def batch_findings(batch_res: pd.DataFrame) -> list[dict]:
    risky = batch_res[batch_res.pred_status == "denied"]
    if risky.empty:
        return []
    fixable = risky[risky.fix_type == FIX_DATA]
    top = risky.pred_code.value_counts()
    code, n = top.index[0], int(top.iloc[0])
    cause, team = CAUSE.get(code, ("", "billing team"))
    return [dict(
        id=f"batch:{len(batch_res)}:{len(risky)}:{len(fixable)}", kind="batch", severity="medium",
        title=f"{len(risky)} of {len(batch_res)} claims waiting to be sent would be rejected",
        what=(f"{len(fixable)} can be fixed here in a few clicks (Check claims tab). "
              f"The most common reason is \"{REASON.get(code, code)}\" ({n} claims)."),
        why=f"Fixing before sending avoids weeks of delay. The most common reason is usually caused by {cause}.",
        action=f"Fix the {len(fixable)} quick fixes now, and share the most common reason with the {team}.",
        team=team, payer=None, code=code, evidence={k: int(v) for k, v in top.items()}, month=None)]


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}


def scan(engine, rules: dict, history: pd.DataFrame, batch_res: pd.DataFrame) -> dict:
    as_of = history.dos.max().normalize()   # latest date of service we have outcomes for
    findings = pattern_findings(history, rules)
    docs, doc_status = document_findings(rules)
    alerts = auth_burn(engine, history, batch_res, as_of)
    findings += docs + auth_finding(alerts, as_of) + batch_findings(batch_res)
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f.get("month") or ""))

    h = history.assign(month=history.dos.dt.to_period("M").astype(str))
    rate = h.groupby("month").apply(lambda x: (x.status == "denied").mean())
    report = dict(
        scanned_at=datetime.now().isoformat(timespec="minutes"),
        data_up_to=str(as_of.date()),
        claims_scanned=int(len(history)),
        batch_size=int(len(batch_res)),
        months=list(rate.index),
        rejection_rate_by_month={k: round(float(v), 3) for k, v in rate.items()},
        documents=doc_status,
        auth_alerts=int(len(alerts)),
        findings=findings,
    )
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=1, default=str))
    return report


def plain_summary(report: dict) -> list[str]:
    """A short written summary that needs no AI: always shown at the top of the Monitor."""
    r = report["rejection_rate_by_month"]
    months = list(r)
    lines = [f"Checked {report['claims_scanned']:,} past claims (up to {report['data_up_to']}), "
             f"{report['documents']['checked']} payer documents and {report['batch_size']} claims waiting to be sent."]
    if len(months) >= 2:
        avg = sum(r.values()) / len(r)
        lines.append(f"About {avg:.0%} of claims are rejected overall "
                     f"({', '.join(f'{pd.Period(m).strftime('%b')} {r[m]:.0%}' for m in months)}).")
    f = report["findings"]
    high = [x for x in f if x["severity"] == "high"]
    lines.append(f"{len(f)} things to look at, {len(high)} of them important."
                 + (f" Most important: {high[0]['title'].lower()}." if high else ""))
    lines.append("No payer documents have changed since the rule list was approved."
                 if report["documents"]["changed"] == 0
                 else f"{report['documents']['changed']} payer document(s) changed: review before the next batch.")
    return lines


# ---------------------------------------------------------------------------
# Suggestion board (decisions people make on findings)
# ---------------------------------------------------------------------------
def load_board() -> dict:
    return json.loads(BOARD_PATH.read_text()) if BOARD_PATH.exists() else {}


def save_decision(finding_id: str, decision: str, note: str = "") -> dict:
    board = load_board()
    board[finding_id] = {"decision": decision, "note": note, "at": datetime.now().isoformat(timespec="minutes")}
    BOARD_PATH.parent.mkdir(exist_ok=True)
    BOARD_PATH.write_text(json.dumps(board, indent=1))
    return board


def clear_board() -> None:
    BOARD_PATH.unlink(missing_ok=True)
