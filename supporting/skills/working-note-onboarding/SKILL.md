---
name: working-note-onboarding
description: Onboards a finance team to Working Note, a coach for financial variance commentary (the Coach menu in their Google Sheet). Interviews the reviewer, labels a sample of the team's past flux commentary, drafts criteria that can each be asked as a yes-or-no question, confirms materiality, and writes the Criteria and Settings tabs plus the labeled calibration set as import-ready files. Use when a team is setting up Working Note (or the Coach) or rewriting its criteria.
---

# Working Note onboarding

You are setting the standard Working Note holds a team's variance commentary to. In the team's Google Sheet it is the Coach menu, so this Skill calls it the Coach. The Coach checks whether commentary can be reviewed against that standard. It never writes or suggests explanations, it never invents feedback (a preparer sees the reviewer's own "Feedback when failed" text, word for word), and it never judges whether an explanation is true. This interview decides what the standard is, in the reviewer's words.

You produce four files, the first three in the sheet's exact column order:

- `criteria.csv`: the Criteria tab
- `settings.csv`: the Settings tab
- `labels.csv`: the labeled sample, which becomes the team's calibration set
- `onboarding_summary.md`: what was decided, what was rejected and why, and what to run next

## Ground rules

- **Ask before reading anything.** Past commentary is real company data, and reading it here sends it to Anthropic, the provider behind Claude. Ask the reviewer to confirm they may share it. If they can't, skip step 1 and start from the default set.
- **The reviewer decides; you draft.** Never mark a line yourself, never write or improve commentary, and never put words in "Feedback when failed" that the reviewer hasn't approved.
- **Criteria are about what the text says**, never about whether it is right. Whether an explanation is true is what the review itself is for.
- Work one step at a time, in short questions, and show your work as small tables.

## Files in this Skill

| File | What it is |
| --- | --- |
| `assets/default_criteria.csv` | The shipped Criteria tab: the default set |
| `assets/default_settings.csv` | The shipped Settings tab |
| `assets/package_example.json` | A complete onboarding package |
| `scripts/scan_history.py` | Lists the usable lines in a folder of past close files |
| `scripts/write_tabs.py` | Checks the package and writes the files |

Both scripts need only Python 3.8 or later (Excel files also need `openpyxl`).

## Step 1. Point at history

Ask for the folder of past flux files. Then run:

```bash
python scripts/scan_history.py <folder> --out candidates.csv
```

It finds tables with an account, prior and current amounts and commentary, and writes one row per line that has commentary, counting repeated text once. A line without both amounts can't be labeled, because Jev reads the row's figures along with the commentary. Files that aren't tables (PDFs, Word files, notes) are listed; read them yourself if they hold commentary with figures.

If the reviewer skips history, there is no sample. Tell them that every criterion, the defaults included, will then stay Uncalibrated, so its flags go to the reviewer as prompts instead of to preparers, and go to step 3.

## Step 2. Label a sample

Show the reviewer the default criteria as a short table (ID, Criterion, Passes when). Then pick 15 to 20 lines from `candidates.csv` that cover the range of the team's work:

- different accounts and sizes of movement, and the balance sheet if the team comments on it;
- weak commentary as well as strong, and first drafts if the files keep them;
- no more than two lines from one account.

Calibration holds out a quarter of each label and needs two passes and two fails held out, so a criterion needs at least 6 lines that fail it and 6 that pass it. A sample of only polished commentary leaves most criteria short of fails. After the first 15 to 20 lines, count each criterion's fails and passes. Where one is short, pick more candidates likely to fill the gap (weak first drafts usually fail several criteria at once) and label those, until every criterion has 6 of each or the reviewer agrees which will stay Uncalibrated for now. The summary `write_tabs.py` writes ends with the same count.

Show each line the way the Flux tab shows it: account, prior, current, change, commentary. Ask which criteria it fails and for one sentence on why. Don't suggest marks: the labels are the reviewer's judgment, and they are what Jev's thresholds are measured against. Active criteria the reviewer leaves unmarked count as passes. Keep each reason in their words, and note any line that fails for a reason no criterion covers.

## Step 3. Ask what's missing

Ask what the reviewer wishes commentary did that it doesn't today. Get two or three concrete answers, each tied to a line they have seen ("show me one where you had to ask"). This keeps the criteria from simply writing down today's habits.

## Step 4. Draft and challenge

Go through the default criteria with the reviewer: keep, edit or retire each one. Then draft new criteria from the reasons in step 2 and the wishes in step 3. Each criterion Jev grades needs:

- **ID**: letters, digits, `-` or `_`, like C6.
- **Criterion**: a short name.
- **Passes when**: one condition the commentary meets, about what the text says ("Names the customer, vendor, contract or hire"). It can also say what doesn't count ("Fails when the cause is a category, even with an amount attached"). Spelling out the near misses is what made C1 reliable.
- **question**: the yes-or-no question that condition asks of a line ("Does the commentary name the customer, vendor, contract or hire?"). If you can't write one, the draft is rejected: tell the reviewer why.
- **Feedback when failed**: what a preparer reads when a line fails, in the reviewer's voice and drawn from the reasons they gave. Read it back; they approve it or rewrite it. It has to stand on its own for any line, so no placeholders and nothing about one particular line.
- **Why it matters** and **Example of good**: optional. Quote the example from a sample line the reviewer marked as passing when you can.

Reject these, and record each in `rejected` with the reason:

- conditions about truth: "the explanation is accurate", "ties to the GL", "is reasonable";
- scores and ratings: "rates clarity from 1 to 5";
- anything that can't be asked as a yes or a no.

Push back on taste words such as "clear", "concise" or "sufficient detail": ask what a reader would see when it is done well, and write that instead.

ARITH, DUP and NEAR are rules in code: the cited amounts add up, the text isn't copied from another line, and it isn't a near-copy. Their wording and feedback can change and they can be retired, but their Passes when describes the code and stays as shipped.

When a criterion is new or reworded after the sample was labeled, go back through the sample and ask which lines fail it. "Unmarked counts as a pass" only holds for criteria the reviewer had in front of them.

Four to seven criteria for Jev is plenty. Each one is another question on every line and another message a preparer may read.

## Step 5. Set materiality

The Coach skips a line when its $ variance is under Materiality ($) and its % variance is under Materiality (%). When the prior amount is zero, the $ test decides alone. The defaults are $25,000 and 10%. Ask the reviewer to confirm or change both, and record both even if they don't change. With `candidates.csv` in hand, tell them how many lines their choice would skip.

## Step 6. Write the tabs

Write the package (below) to `package.json` and run:

```bash
python scripts/write_tabs.py package.json --out onboarding
```

An error means nothing was written. Resolve each one with the reviewer, and never reword a criterion just to get past a check without telling them. Read the warnings out too. Then show the reviewer the Criteria rows and the readiness table, and hand over the folder. `onboarding_summary.md` lists the next steps:

1. From the root of the Working Note repo, record Jev's answers for the sample and calibrate. Recording sends each labeled line's account, comparison basis, amounts and commentary to Jev and costs a fraction of a cent:

   ```bash
   python scripts/record_jev.py --labels onboarding/labels.csv --criteria onboarding/criteria.csv \
       --recordings onboarding/team_recordings.jsonl --yes
   python scripts/calibrate.py --labels onboarding/labels.csv --criteria onboarding/criteria.csv \
       --recordings onboarding/team_recordings.jsonl --out onboarding/team_calibration.json --write-db
   python <this skill>/scripts/write_tabs.py package.json --out onboarding \
       --calibration onboarding/team_calibration.json
   ```

2. In the sheet, open the Criteria tab, then File > Import > Upload `criteria.csv` with Import location "Replace current sheet". Do the same on the Settings tab with `settings.csv`. Then run Coach > Admin > Set up sheet, which puts back the dropdowns and protections, and Coach > Sync criteria status.

When the files came from the chat prompt instead (`supporting/onboarding_prompt.md` in the repo), `python scripts/write_tabs.py --check <folder>` runs the same checks on `criteria.csv`, `settings.csv` and `labels.csv`.

## The package

`assets/package_example.json` is a complete one. Its fields:

| Field | What it holds |
| --- | --- |
| `team` | Optional. The team's name, for the summary |
| `owner` | Who owns the criteria. New criteria get this Owner |
| `reviewed_on` | The interview date, `YYYY-MM-DD`; every row's Last reviewed. Defaults to today |
| `criteria` | One entry per criterion (below) |
| `rejected` | Drafts that didn't survive step 4: `{"draft": ..., "why": ...}` |
| `settings` | Settings tab labels to values. Must include `Materiality ($)` and `Materiality (%)` |
| `sample` | The labeled lines (below) |

A criterion entry is either a default, `{"id": "C1", "from_default": true}`, optionally with edited fields or `"active": false` to retire it, or a full criterion with `id`, `criterion`, `passes_when`, `feedback`, `why`, `example`, `owner`, `question` and `active`.

Every default criterion must appear: kept, edited or retired. A retired criterion stays in the tab with Active N, so Criteria History records the decision. ARITH, DUP and NEAR are added as shipped when you leave them out.

A default kept with the same Passes when keeps its shipped threshold as a starting value, and every criterion, kept or new, is Uncalibrated until `calibrate.py` measures it on the team's own sample. A new or reworded criterion starts with no threshold.

A sample line has `text_id` (S01, S02, ... if left out), `account`, `comparison_basis` (MoM unless the line compares to a quarter, a year, a budget or a forecast: QoQ, YoY, vs Budget, vs Forecast), `prior` and `current` as numbers, `commentary` exactly as written, `fails` (the criterion IDs the reviewer marked), `reason` (their sentence; required when anything fails) and `source` (the file and row, for the summary).
