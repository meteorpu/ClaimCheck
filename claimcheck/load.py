"""Load and clean the data pack.

Exports are "as-is" from billing systems, so this step standardises them and
records every change it makes in an `issues` column, so the biller can see
what was cleaned (data-integrity check) rather than it happening silently.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CLAIMCHECK_DATA", ROOT / "Resonance - graduate preparation data"))
RULES_PATH = Path(__file__).resolve().parent / "rules.yaml"

KNOWN_SERVICES = {"T1019", "T1005", "S5125"}


def load_rules(path: Path = RULES_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _payer_lookup(rules: dict) -> dict[str, str]:
    lookup = {}
    for pid, names in rules["payer_aliases"].items():
        for n in names:
            lookup[n.strip().upper()] = pid
    return lookup


def parse_date(value) -> pd.Timestamp | None:
    """Exports mix ISO (2026-06-09) and US (06/09/2026) dates."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if not s:
        return None
    fmt = "%m/%d/%Y" if "/" in s else "%Y-%m-%d"
    try:
        return pd.to_datetime(s, format=fmt)
    except ValueError:
        return None


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""


def clean_claims(raw: pd.DataFrame, rules: dict) -> pd.DataFrame:
    payers = _payer_lookup(rules)
    rows = []
    for _, r in raw.iterrows():
        issues: list[str] = []

        payer_raw = "" if _blank(r["payer"]) else str(r["payer"]).strip()
        payer_id = payers.get(payer_raw.upper())
        if payer_id is None:
            issues.append(f"unknown payer '{payer_raw}'")
        elif payer_raw != payer_id:
            issues.append(f"payer name '{payer_raw}' normalised to {payer_id}")

        svc_raw = "" if _blank(r["service_code"]) else str(r["service_code"])
        svc = svc_raw.strip().upper()
        if svc != svc_raw:
            issues.append("service code had stray spaces")
        if svc not in KNOWN_SERVICES:
            issues.append(f"unknown service code '{svc}'")

        dates = {}
        for col in ("date_of_service", "submitted_date"):
            d = parse_date(r[col])
            if d is None:
                issues.append(f"unreadable {col}")
            elif "/" in str(r[col]):
                issues.append(f"{col} in US format, converted")
            dates[col] = d

        mod = None if _blank(r["modifier"]) else str(r["modifier"]).strip().upper()
        if mod is not None and mod != str(r["modifier"]):
            issues.append("modifier case/spacing fixed")

        auth = None if _blank(r["auth_id"]) else str(r["auth_id"]).strip().upper()

        try:
            units = int(float(r["units"]))
        except (TypeError, ValueError):
            units = 0
            issues.append("units not a number")
        if units <= 0:
            issues.append("units must be positive")

        freq = "1" if _blank(r["frequency_code"]) else str(r["frequency_code"]).strip()
        orig = None if _blank(r.get("original_claim_id")) else str(r["original_claim_id"]).strip()
        if freq == "7" and orig is None:
            issues.append("corrected claim (freq 7) has no original_claim_id")

        row = {
            "claim_id": str(r["claim_id"]).strip(),
            "agency_id": str(r["agency_id"]).strip(),
            "payer_id": payer_id,
            "patient_id": str(r["patient_id"]).strip(),
            "service_code": svc,
            "dos": dates["date_of_service"],
            "submitted": dates["submitted_date"],
            "units": units,
            "auth_id": auth,
            "modifier": mod,
            "frequency_code": freq,
            "original_claim_id": orig,
            "billed_amount": float(r["billed_amount"]) if not _blank(r["billed_amount"]) else None,
            "issues": issues,
        }
        for col in ("status", "paid_amount", "denial_code", "remark"):
            if col in raw.columns:
                row[col] = None if _blank(r[col]) else str(r[col]).strip()
        rows.append(row)
    return pd.DataFrame(rows)


def load_claims(name: str, rules: dict) -> pd.DataFrame:
    raw = pd.read_csv(DATA_DIR / name, dtype=str)
    before = len(raw)
    raw = raw.drop_duplicates()  # the history export contains exact duplicate rows
    df = clean_claims(raw, rules)
    df.attrs["dropped_exact_duplicates"] = before - len(raw)
    return df


def load_evv() -> pd.DataFrame:
    e = pd.read_csv(DATA_DIR / "evv_visits.csv", dtype=str)
    e["service_code"] = e["service_code"].str.strip().str.upper()
    e["clock_in"] = pd.to_datetime(e["clock_in"])
    e["clock_out"] = pd.to_datetime(e["clock_out"])
    e["minutes"] = (e["clock_out"] - e["clock_in"]).dt.total_seconds() / 60
    e["date"] = e["clock_in"].dt.normalize()  # a visit belongs to its clock-in date
    e["verification_status"] = e["verification_status"].str.strip().str.lower()
    return e


def load_auths() -> pd.DataFrame:
    a = pd.read_csv(DATA_DIR / "authorizations.csv", dtype=str)
    a["auth_id"] = a["auth_id"].str.strip().str.upper()
    a["service_code"] = a["service_code"].str.strip().str.upper()
    a["start_date"] = pd.to_datetime(a["start_date"])
    a["end_date"] = pd.to_datetime(a["end_date"])
    a["units_authorized"] = a["units_authorized"].astype(int)
    return a
