"""Check the engine against claims_history.csv.

    python backtest.py               # accuracy report
    python backtest.py --precedence  # which code payers report when several rules break
    python backtest.py --errors D09  # show mismatches for one code
"""
import argparse
from collections import Counter
from pathlib import Path

import pandas as pd

from claimcheck.load import load_claims
from claimcheck.pipeline import backtest, build

OUT = Path(__file__).parent / "output"


def report(df: pd.DataFrame) -> None:
    actual_code = df.denial_code.fillna("PAID")
    pred_code = df.pred_code.fillna("PAID")
    n = len(df)
    status_acc = (df.status == df.pred_status).mean()
    code_acc = (actual_code == pred_code).mean()
    d99 = actual_code == "D99"
    code_acc_ex99 = (actual_code[~d99] == pred_code[~d99]).mean()

    tp = ((df.status == "denied") & (df.pred_status == "denied")).sum()
    fp = ((df.status == "paid") & (df.pred_status == "denied")).sum()
    fn = ((df.status == "denied") & (df.pred_status == "paid")).sum()
    print(f"Claims back-tested: {n}  (after dropping {df.attrs.get('dropped', 0)} exact duplicate rows)")
    print(f"Pay/deny accuracy:          {status_acc:.1%}")
    print(f"Denial precision:           {tp / (tp + fp):.1%}   (when we flag a claim, how often it was really denied)")
    print(f"Denial recall:              {tp / (tp + fn):.1%}   (of real denials, how many we caught)")
    print(f"Exact outcome+code match:   {code_acc:.1%}")
    print(f"  excluding D99 (n={d99.sum()}): {code_acc_ex99:.1%}   (D99 = 'contact payer / under review', not predictable from rules)")
    print()
    rows = []
    for code in sorted(set(actual_code) | set(pred_code)):
        a = actual_code == code
        p = pred_code == code
        rows.append({"code": code, "actual": a.sum(), "predicted": p.sum(), "correct": (a & p).sum(),
                     "recall": (a & p).sum() / a.sum() if a.sum() else float("nan"),
                     "precision": (a & p).sum() / p.sum() if p.sum() else float("nan")})
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.0%}"))
    print()
    print("Confusion (rows = actual, cols = predicted):")
    print(pd.crosstab(actual_code, pred_code))


def precedence(df: pd.DataFrame) -> None:
    """For claims breaking 2+ rules, which one did the payer report?"""
    wins = Counter()
    for _, r in df[df.all_codes.str.len() > 1].iterrows():
        codes = set(r.all_codes)
        if r.denial_code in codes:
            for other in codes - {r.denial_code}:
                wins[(r.denial_code, other)] += 1
    print("reported-over counts (A beats B):")
    for (a, b), n in sorted(wins.items()):
        rev = wins.get((b, a), 0)
        print(f"  {a} over {b}: {n}   ({b} over {a}: {rev})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--precedence", action="store_true")
    ap.add_argument("--errors")
    args = ap.parse_args()

    rules, engine, history = build()
    batch = load_claims("claims_to_check.csv", rules)
    df = backtest(engine, history, batch)
    df.attrs["dropped"] = history.attrs.get("dropped_exact_duplicates", 0)
    OUT.mkdir(exist_ok=True)
    df.drop(columns=["patch", "all_problems", "issues"]).to_csv(OUT / "backtest_results.csv", index=False)

    if args.precedence:
        precedence(df)
    elif args.errors:
        code = None if args.errors == "PAID" else args.errors
        m = df[(df.denial_code.fillna("PAID") == args.errors) != (df.pred_code.fillna("PAID") == args.errors)]
        cols = ["claim_id", "payer_id", "service_code", "dos", "submitted", "units", "auth_id", "modifier",
                "status", "denial_code", "remark", "pred_code", "all_codes", "reason"]
        print(m[cols].head(40).to_string())
        print(len(m), "mismatches")
    else:
        report(df)
