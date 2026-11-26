"""Predict outcomes for the unsubmitted claims.

    python predict.py                      # rules only (free, deterministic)
    python predict.py --explain            # + LLM explanations for flagged claims (needs ANTHROPIC_API_KEY)
    python predict.py --file my_batch.csv  # any batch with the same columns

Writes output/predictions.csv (claim_id,status,denial_code) and output/results_table.csv.
"""
import argparse
from pathlib import Path

from dotenv import load_dotenv

from claimcheck.load import load_claims
from claimcheck.pipeline import build, predict_batch

OUT = Path(__file__).parent / "output"

if __name__ == "__main__":
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="claims_to_check.csv")
    ap.add_argument("--explain", action="store_true")
    args = ap.parse_args()

    rules, engine, history = build()
    batch = load_claims(args.file, rules)
    res = predict_batch(engine, history, batch)

    if args.explain:
        from claimcheck.explain import Explainer
        ex = Explainer()
        if not ex.enabled:
            print("ANTHROPIC_API_KEY not set: skipping explanations")
        else:
            flagged = res[res.pred_code.notna()]
            print(f"Explaining {len(flagged)} flagged claims with {ex.model}...")
            notes = {}
            for c in flagged.to_dict("records"):
                e = ex.explain(c)
                if e:
                    notes[c["claim_id"]] = e
            res["ai_summary"] = res.claim_id.map(lambda i: notes[i].summary if i in notes else "")
            res["ai_next_step"] = res.claim_id.map(lambda i: notes[i].next_step if i in notes else "")
            res["ai_agrees"] = res.claim_id.map(lambda i: notes[i].agrees_with_engine if i in notes else None)
            res["ai_reviewer_note"] = res.claim_id.map(lambda i: notes[i].reviewer_note if i in notes else "")
            print("LLM usage:", ex.usage)

    OUT.mkdir(exist_ok=True)
    res.rename(columns={"pred_status": "status", "pred_code": "denial_code"})[["claim_id", "status", "denial_code"]] \
        .to_csv(OUT / "predictions.csv", index=False)
    res.drop(columns=["patch", "all_problems"]).to_csv(OUT / "results_table.csv", index=False)

    n = len(res)
    flagged = res.pred_code.notna().sum()
    print(f"{n} claims checked: {n - flagged} predicted paid, {flagged} predicted denied")
    print(res[res.pred_code.notna()].groupby(["pred_code", "fix_type"]).size().to_string())
    print(f"Wrote {OUT / 'predictions.csv'}")
