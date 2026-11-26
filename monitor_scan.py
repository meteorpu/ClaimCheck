"""Run the Monitor scan from the command line, so it can run on a schedule.

    .venv/bin/python monitor_scan.py             # scan, print the summary, save output/monitor_report.json
    .venv/bin/python monitor_scan.py --brief     # also write an AI briefing (needs ANTHROPIC_API_KEY)

Example schedule (every weekday at 7am), via `crontab -e`:
    0 7 * * 1-5  cd /path/to/resonance && .venv/bin/python monitor_scan.py
The app shows the latest saved report and can re-run the scan on demand.
"""
import argparse

from dotenv import load_dotenv

from claimcheck import monitor
from claimcheck.explain import write_briefing
from claimcheck.load import load_claims
from claimcheck.pipeline import build, predict_batch

if __name__ == "__main__":
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true", help="write an AI briefing of the findings")
    args = ap.parse_args()

    rules, engine, history = build()
    batch = predict_batch(engine, history, load_claims("claims_to_check.csv", rules))
    report = monitor.scan(engine, rules, history, batch)

    print("\n".join(monitor.plain_summary(report)))
    print()
    for f in report["findings"]:
        print(f"[{f['severity']}] {f['title']}\n    {f['what']}\n    -> {f['action']}")
    if args.brief:
        text = write_briefing(report)
        print("\n" + (text or "(No AI briefing: set ANTHROPIC_API_KEY in .env)"))
    print(f"\nSaved {monitor.REPORT_PATH}")
