"""Batch processing over one shared timeline.

History and new claims overlap in time (new claims are submitted between
history claims), and they draw down the same authorizations. So both are
replayed together in submission order through one Ledger:
  - history claims: predicted (for the back-test), then recorded with their REAL outcome
  - new claims:     predicted, then recorded with their PREDICTED outcome
Every claim therefore sees every earlier claim, including earlier claims in the same batch.
"""
from __future__ import annotations

import pandas as pd

from .engine import FIX_DATA, FIX_NONE, FIX_PROCESS, Engine, Ledger, records
from .load import load_auths, load_claims, load_evv, load_rules


def build(precedence=None):
    rules = load_rules()
    engine = Engine(rules, load_evv(), load_auths(), precedence)
    history = load_claims("claims_history.csv", rules)
    return rules, engine, history


SEVERITY = {FIX_DATA: 0, FIX_PROCESS: 1, FIX_NONE: 2}


def _row(c, status, code, v, engine=None, ledger=None):
    top = v[0] if v else None
    fix_type, fix, patch = (top.fix_type, top.fix, top.patch) if top else ("", "", {})
    if top and engine is not None:
        # Overall fix: try every data fix in turn; whatever is left decides who must act.
        patch, steps, remaining = engine.propose_fix(c, ledger)
        if not remaining:
            fix_type, fix = FIX_DATA, " + ".join(steps)
        else:
            worst = max(remaining, key=lambda x: SEVERITY[x.fix_type])
            fix_type = worst.fix_type
            fix = (" + ".join(steps) + ", then: " if steps else "") + worst.fix
    return {
        "claim_id": c["claim_id"],
        "pred_status": status,
        "pred_code": code,
        "reason": top.reason if top else "",
        "rule_source": top.source if top else "",
        "fix_type": fix_type,
        "suggested_fix": fix,
        "patch": patch,
        "all_codes": [x.code for x in v],
        "all_problems": [{"code": x.code, "reason": x.reason, "source": x.source, "fix_type": x.fix_type,
                          "fix": x.fix, "patch": x.patch} for x in v],
    }


def run(engine: Engine, history: pd.DataFrame, batch: pd.DataFrame | None = None):
    """Returns (history_with_predictions, batch_with_predictions)."""
    batch = batch if batch is not None else history.iloc[0:0]
    timeline = pd.concat([history.assign(_batch=False), batch.assign(_batch=True)], ignore_index=True)
    ledger = Ledger()
    hist_out, batch_out = [], []
    for c in records(timeline):
        status, code, v = engine.predict(c, ledger)
        if c["_batch"]:
            batch_out.append(_row(c, status, code, v, engine, ledger))
            ledger.record(c, status)
        else:
            hist_out.append(_row(c, status, code, v))
            ledger.record(c, c["status"])
    h = history.merge(pd.DataFrame(hist_out), on="claim_id") if hist_out else history
    b = batch.merge(pd.DataFrame(batch_out), on="claim_id") if batch_out else batch
    return h, b


def backtest(engine: Engine, history: pd.DataFrame, batch: pd.DataFrame | None = None) -> pd.DataFrame:
    return run(engine, history, batch)[0]


def predict_batch(engine: Engine, history: pd.DataFrame, batch: pd.DataFrame) -> pd.DataFrame:
    return run(engine, history, batch)[1]
