"""Unit tests for the rules that are easiest to get subtly wrong."""
import pandas as pd
import pytest

from claimcheck.engine import FIX_DATA, Engine, Ledger, units_from_minutes
from claimcheck.load import clean_claims, load_rules, parse_date

RULES = load_rules()
D = pd.Timestamp


def make_engine(visits=(), auths=()):
    evv = pd.DataFrame(list(visits), columns=["visit_id", "patient_id", "service_code", "date", "minutes", "verification_status"])
    au = pd.DataFrame(list(auths), columns=["auth_id", "patient_id", "payer_id", "service_code", "start_date", "end_date", "units_authorized"])
    return Engine(RULES, evv, au)


def claim(**kw):
    c = dict(claim_id="C1", payer_id="PY01", patient_id="P1", service_code="T1005", dos=D("2026-09-10"),
             submitted=D("2026-09-15"), units=8, auth_id="A1", modifier=None, frequency_code="1", original_claim_id=None)
    c.update(kw)
    return c


AUTH = ("A1", "P1", "PY01", "T1005", D("2026-09-01"), D("2026-11-30"), 100)


def visit(minutes, status="verified", dos="2026-09-10", payer_svc="T1005"):
    return ("V1", "P1", payer_svc, D(dos), minutes, status)


def codes(engine, c, ledger=None):
    return [v.code for v in engine.violations(c, ledger or Ledger())]


# ---- rounding (D09) ----
@pytest.mark.parametrize("minutes,rule,expected", [
    (264, "eight_minute_rule", 18), (262, "eight_minute_rule", 17),   # 9 vs 7 leftover minutes
    (264, "whole_units_only", 17), (59, "whole_units_only", 3),
])
def test_rounding(minutes, rule, expected):
    assert units_from_minutes(minutes, rule) == expected


def test_same_units_pass_northwind_but_fail_harbor():
    e = make_engine([visit(264)], [AUTH, ("A2", "P1", "PY02", "T1005", D("2026-09-01"), D("2026-11-30"), 100)])
    assert codes(e, claim(units=18)) == []
    assert codes(e, claim(units=18, payer_id="PY02", auth_id="A2")) == ["D09"]


# ---- EVV (D01) ----
def test_pending_evv_does_not_count():
    e = make_engine([visit(120, status="manual_edit_pending")], [AUTH])
    assert codes(e, claim()) == ["D01"]


# ---- timely filing (D03) uses the rule in force on the date of service ----
def test_northwind_bulletin_extends_limit_from_august():
    e = make_engine([visit(120, dos="2026-07-20"), visit(120, dos="2026-08-20")],
                    [("A1", "P1", "PY01", "T1005", D("2026-06-01"), D("2026-11-30"), 100)])
    old = claim(dos=D("2026-07-20"), submitted=D("2026-07-20") + pd.Timedelta(days=40))
    new = claim(dos=D("2026-08-20"), submitted=D("2026-08-20") + pd.Timedelta(days=40))
    assert "D03" in codes(e, old)       # 30-day limit before 1 Aug
    assert "D03" not in codes(e, new)   # 45-day limit from 1 Aug


# ---- Harbor weekend modifier (D04) ----
def test_harbor_weekend_modifier_after_september():
    a = ("A2", "P1", "PY02", "T1019", D("2026-06-01"), D("2026-11-30"), 500)
    e = make_engine([visit(120, dos="2026-09-12", payer_svc="T1019"), visit(120, dos="2026-08-15", payer_svc="T1019")], [a])
    sat_sep = claim(payer_id="PY02", service_code="T1019", auth_id="A2", dos=D("2026-09-12"), modifier="U2")
    sat_aug = claim(payer_id="PY02", service_code="T1019", auth_id="A2", dos=D("2026-08-15"), modifier="U2")
    assert codes(e, sat_sep) == ["D04"]   # Saturday after 1 Sep needs U3
    assert codes(e, sat_aug) == []        # before the bulletin U2 is right


# ---- Summit contract amendment (D10) ----
def test_summit_whole_hours_from_july():
    a = ("A3", "P1", "PY03", "T1005", D("2026-06-01"), D("2026-11-30"), 500)
    e = make_engine([visit(120, dos="2026-06-10"), visit(120, dos="2026-07-10")], [a])
    june = claim(payer_id="PY03", auth_id="A3", dos=D("2026-06-10"), submitted=D("2026-06-15"), units=6)
    july = claim(payer_id="PY03", auth_id="A3", dos=D("2026-07-10"), submitted=D("2026-07-15"), units=6)
    assert codes(e, june) == []        # 6 units = 1.5 hours: fine before the amendment
    assert codes(e, july) == ["D10"]   # must be whole hours from 1 July


# ---- authorization drawdown depends on earlier claims (D02) ----
def test_drawdown_uses_ledger():
    e = make_engine([visit(600, dos="2026-09-10"), visit(600, dos="2026-09-11")],
                    [("A1", "P1", "PY01", "T1005", D("2026-09-01"), D("2026-11-30"), 30)])
    ledger = Ledger()
    first = claim(claim_id="C1", units=20, dos=D("2026-09-10"))
    second = claim(claim_id="C2", units=20, dos=D("2026-09-11"))
    assert codes(e, second, ledger) == []        # on its own it fits
    ledger.record(first, "paid")                 # an earlier claim uses 20 of 30 units
    assert codes(e, second, ledger) == ["D02"]   # now only 10 are left


# ---- duplicates vs corrected claims (D05) ----
def test_duplicate_of_denied_claim_can_become_corrected_claim():
    e = make_engine([visit(120)], [AUTH])
    ledger = Ledger()
    ledger.record(claim(claim_id="C0"), "denied")
    v = e.violations(claim(claim_id="C1"), ledger)
    assert v[0].code == "D05" and v[0].fix_type == FIX_DATA
    assert v[0].patch == {"frequency_code": "7", "original_claim_id": "C0"}
    assert codes(e, claim(claim_id="C1", **v[0].patch), ledger) == []


# ---- missing authorization: only propose one that exists (D07) ----
def test_missing_auth_proposes_existing_auth_only():
    e = make_engine([visit(120)], [AUTH])
    v = e.violations(claim(auth_id=None), Ledger())
    assert v[0].code == "D07" and v[0].patch == {"auth_id": "A1"}
    e2 = make_engine([visit(120)], [])
    v2 = e2.violations(claim(auth_id=None), Ledger())
    assert v2[0].code == "D07" and v2[0].patch == {}   # never invents an authorization


# ---- cleaning ----
def test_cleaning_normalises_messy_exports():
    raw = pd.DataFrame([dict(claim_id="C9", agency_id="AG1", payer="NORTHWIND HP", patient_id="P1", service_code="T1019 ",
                             date_of_service="09/13/2026", submitted_date="2026-09-29", units="12", auth_id=None,
                             modifier=" u1", frequency_code="1", original_claim_id=None, billed_amount="64.8")])
    c = clean_claims(raw, RULES).iloc[0]
    assert c.payer_id == "PY01" and c.service_code == "T1019" and c.modifier == "U1"
    assert c.dos == D("2026-09-13") and len(c.issues) == 4
    assert parse_date("06/09/2026") == D("2026-06-09")   # US month/day
