# C1 blind check (2026-10-11)

Why C1's pass condition changed, and the evidence that the new wording is better.

## The problem

Under the spec's wording ("Points to a vendor, contract, hire, customer, or event, not a category like "timing" or "growth""), Jev's C1 probability rose with any added detail. A vendor name, an amount or a date lifted a vague driver past the threshold. So "a one-off data load during the month" failed at 0.88 against 0.91, but the same line with "($48.5K, Sep 9 to Sep 24)" passed. Specific drivers that aren't commercial, such as an FX move on a named balance or the Social Security wage base, failed. Of September's 32 C1 answers, 28 sat within 0.05 of the threshold.

## The candidate

A separate session investigated the problem and drafted the new wording after seeing which shipped texts C1 got wrong:

> Names the specific cause of the change, identified well enough that a reviewer could look it up: which customer, vendor, contract, hire or event, and what changed with it. Fails when the cause is a category ("timing", "growth", "higher volume"), even with an amount or dates attached, or when the only name given is where the cost was booked or who billed it.

It scored perfectly on the 112 shipped labels (AUC 1.000, against 0.957 for the old wording). That score is partly in-sample, so it couldn't decide the question.

## The blind check

Written by a session that didn't draft the candidate, in this order:

1. `PREREGISTRATION.md`: the comparison, the label rule, the thresholds (old wording at 0.91, new at 0.47) and the three rules for shipping.
2. `blind_labels.csv`: 36 new texts, 18 labeled pass and 18 fail. They include the hard kinds on purpose: vendor-name-only lines, and specific drivers that aren't commercial.
3. `FINGERPRINTS.txt`: SHA-256 of both files, taken before anything was sent to Jev. Check them with `shasum -a 256 PREREGISTRATION.md blind_labels.csv`.
4. `blind_recordings.jsonl`: 72 real Jev answers (36 texts under each wording), snapshot jev-1.13-20260917, data collection denied. OpenRouter reported $0.0016.
5. `RESULTS.txt`, from `score_blind.py`, which reproduces it from the files here:

```bash
.venv/bin/python docs/evidence/c1-blind-check/score_blind.py
```

| | Old wording at 0.91 | New wording at 0.47 |
| --- | --- | --- |
| Balanced accuracy | 0.778 | 0.972 |
| AUC | 0.921 | 0.997 |
| Fails caught | 15 of 18 | 17 of 18 |
| Passes kept | 13 of 18 | 18 of 18 |
| Answers within 0.05 of the threshold | 18 of 36 | 0 of 36 |

All three rules held, so the new wording shipped. Its one miss (BLIND-31, 0.59) names only the vendor that billed the account, plus an amount. That is the weak spot that remains.

## After shipping

- Recalibrated on the 112 shipped labels, C1's threshold is 0.43; the blind result is identical at 0.43. The other criteria are unchanged.
- The synthetic reviewer's derived "flag was wrong" overrides on C1 fell from 8 to 0 (20 derived overrides in all, now 12).
- The demo line's first draft fails at 0.30 against 0.43, so the demo no longer turns on a 0.03 margin. Second attempts are still 15, 9 and 5 across the three closes, and every criterion still falls each close.

## Read with care

The blind texts were written by someone who had read the memo that motivated the change, so they deliberately cover the failure modes it describes. They are synthetic, and cleaner than a real team's commentary. A team's own labeled lines, from onboarding, are what make a threshold real.
