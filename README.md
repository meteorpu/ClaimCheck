# ClaimCheck: catch rejected claims before they are sent

An AI claims assistant for home care agency billing teams (Exchange4Health case study).

Upload a batch of claims and get one table back: for each claim, whether it will be paid or rejected, why (in plain words, with the
payer rule behind it), and what to do. Simple corrections are proposed from existing records and approved by the biller with one click.
A Monitor scans past outcomes and payer documents for rule changes people would not notice on their own.

**Rules decide, AI explains, a person approves.**

- System design (architecture, workflows, functionality checklist): [`docs/system-design.md`](docs/system-design.md)
- Design sketch: [`docs/design.html`](docs/design.html)

## Results

Back-test on `claims_history.csv` (3,830 claims after removing 32 duplicate rows), each predicted only from what was known before it was sent:

| Pay / reject accuracy | Precision | Recall | Exact code | Exact code, excl. D99 |
|---|---|---|---|---|
| **98.4%** | **99.9%** | 94.4% | 97.9% | **99.8%** |

The recall gap is almost all D99 ("referred for manual review"), which no rule can predict.

On `claims_to_check.csv` (546 claims): **377 ready to send, 169 likely rejected** ($17.8k of $54.1k at risk). Of the 169, 48 can be
fixed in the app, 62 need another team, 59 cannot be fixed. Output: `output/predictions.csv` (`claim_id,status,denial_code`).

## How to run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py          # the app
.venv/bin/python predict.py             # writes output/predictions.csv
.venv/bin/python backtest.py            # accuracy report
.venv/bin/python monitor_scan.py        # Monitor scan (schedule with cron)
.venv/bin/python -m pytest tests        # 13 unit tests
```

AI features (optional): put `ANTHROPIC_API_KEY=...` in a `.env` file (git-ignored). Without it, everything except the two AI buttons works.

## Using the app

| Tab | Who | What you do |
|---|---|---|
| 📋 Check claims | Billers | Upload claims, filter the results, download `predictions.csv`, open a claim to approve its fix or ask AI to explain it |
| 🔔 Monitor | Billing manager | Read the last scan, accept / postpone / dismiss suggestions, view one trend graph at a time |
| 🎯 How accurate is it? | Reviewers | Back-test results and every claim it got wrong |
| 📖 Payer rules | Everyone | Every rule in plain words, its dates, source, and whether it is confirmed |

## Findings and assumptions

- **The two claim files are one timeline.** Claims to send fall between history claims and use the same authorizations; replaying
  both in order through one ledger explained 10 otherwise-missed D02 rejections.
- **Summit has an undocumented rule.** From 1 Jul 2026 it rejects units that are not whole hours (D10, "contract amendment 3"). Found
  from rejection notes, marked unconfirmed; assumed to apply to all agencies.
- **Which code is reported when several rules break** was learned from history (`D01 → D10 → D09 → D08 → D07 → D04 → D05 → D06 → D02 → D03`).
- **Data quirks:** 12 Summit claims coded D99 cite amendment 3 (counted as errors); about 8 claims are inconsistent between D07/D08 and D09.
- **Assumptions:** slash dates are US format; a visit belongs to its clock-in date and same-day visits are summed before rounding;
  hours are used in submission order; corrected claims (frequency 7) are never duplicates.
- **Caveat:** the code order and Summit rule were learned from the same history the back-test scores; a time-split test would be stricter.

## Next steps

1. Integrate LLM to extract and draft rule-list changes from new bulletins or a new payer's manual; a person approves.
2. Monitor fetches payer documents from official websites and notifies each role.
3. Writing assistant: build claims from verified visits so most errors never happen.