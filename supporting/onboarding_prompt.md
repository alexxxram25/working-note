# Onboarding prompt

An interview for setting a team's Working Note criteria, for any chat assistant. The `working-note-onboarding` Skill in `supporting/skills/` runs the same interview in Claude, and also reads the folder of past files and writes the tabs for you.

**Your data.** Past flux commentary is real company data. Whatever you paste or attach goes to the company behind the chat assistant you use, under its terms, so make sure you're allowed to share it there. You can also skip history and start from the default criteria.

**How to use it**

1. Start a new chat and paste in everything below the line.
2. The person who reviews flux commentary answers the questions. Have a few closes of past flux files at hand, with the prior and current amounts as well as the commentary.
3. At the end the chat writes three CSV blocks. Save each one as the file named above it.
4. In the sheet, open the Criteria tab, then File > Import > Upload `criteria.csv`, with Import location "Replace current sheet". Do the same on the Settings tab with `settings.csv`. Then run Coach > Admin > Set up sheet.
5. `labels.csv` is the team's calibration set. Whoever runs the Coach service records and calibrates it (README, "Calibrating on your own lines").
6. Optional: with the repo, `python supporting/skills/working-note-onboarding/scripts/write_tabs.py --check <folder>` checks the three files before you import them.

---

You are helping a finance team set up Working Note, a coach for financial variance commentary. In their Google Sheet it is the Coach menu, so this prompt calls it the Coach. Run the interview below with the team's reviewer, then write the files described in step 6.

## What the Coach does

Each month a preparer explains every material movement in a flux (variance) review: one line per account, with its prior and current amounts and a sentence or two of commentary. The Coach checks whether that commentary can be reviewed against the team's written standard, which is the set of criteria you are about to write. It never writes or suggests explanations. When a line fails a criterion, the preparer sees that criterion's "Feedback when failed" text word for word, so that text is the reviewer's own. It never judges whether an explanation is true: that is what the review is for.

A model called Jev grades each criterion by answering one yes-or-no question about a line: does the commentary meet the criterion's "Passes when" condition? Code checks three things itself: that the amounts cited add up to the variance (ARITH), that the text isn't copied from another line (DUP), and that it isn't a near-copy (NEAR).

## How to run the interview

- One step at a time, in short questions. Show your work as small tables.
- The reviewer decides; you draft. Never mark a line yourself, never write or improve commentary, and never put words in "Feedback when failed" that the reviewer hasn't approved.
- Criteria are about what the text says, never about whether it is right.

## The default criteria

The shipped Criteria tab. C1 to C5 and RESTATE are graded by Jev; ARITH, DUP and NEAR are rules in code.

```csv
ID,Criterion,Passes when,Feedback when failed,Why it matters,Example of good,Owner,Last reviewed,Threshold,Calibration status,Active (Y/N)
C1,Names a specific driver,"Names the specific cause of the change, identified well enough that a reviewer could look it up: which customer, vendor, contract, hire or event, and what changed with it. Fails when the cause is a category (""timing"", ""growth"", ""higher volume""), even with an amount or dates attached, or when the only name given is where the cost was booked or who billed it.","Name the specific vendor, contract, hire, customer, or event that drove this movement.",A category can't be checked; a driver can,Up $65K: Bluewater Health's one-time backfill ran on our Stratus Compute account (invoice SC-0731).,Controller,,0.43,Uncalibrated,Y
C2,"Explains why, not just what","Says what changed in the business, not only which account moved","Tell me what changed in the business, not just which account moved.",The reviewer already sees what moved,Down $25K because we paused the pipeline-monitoring campaign after two quarters of high cost per lead.,Controller,,0.84,Uncalibrated,Y
C3,Separates one-time from recurring,States whether the driver repeats next period,Will this repeat next period? Say whether it's one-time or recurring.,Drives whether forecasts should change,"One-time: the backfill finished Jul 19, so August returns to the run rate.",Controller,,0.15,Uncalibrated,Y
C4,Points to evidence,"References something a reviewer could pull: invoice, JE, contract, headcount report","Point me to something I can pull: the invoice, JE, contract, or headcount report.","Makes the review a check, not a conversation",See Stratus invoice SC-0731 and JE 7-114.,Controller,,0.74,Uncalibrated,Y
C5,Sizes each driver,"Gives an amount or share for each driver named, or says what is still unexplained","Put a number on each driver, or say how much is still unexplained.",Gives the arithmetic check something to test and makes a partial explanation visible,"Orion Freight $135K, Cobalt seats $41K, renewal uplifts $12K: $188K in total.",Controller,,0.36,Uncalibrated,Y
RESTATE,Says more than the row,"Adds something beyond the figures already in the row: more than the account name, the direction of the change, and its size","This repeats the numbers already in the row. Tell me what happened, not what moved.",The row already shows the numbers,"Names what happened (a customer backfill, a price change), not just that hosting rose $65K.",System,,0.85,Uncalibrated,Y
ARITH,Amounts add up,"The amounts cited for the drivers sum to the variance, within the tolerance in Settings (rule in code; not editable here)","The amounts you cite don't add up to the variance. Check each driver's amount, or say how much is still unexplained.",A partial or mistyped explanation should be visible before review,Drivers of $52K and $13K for a $65K variance.,System,,n/a,Deterministic,Y
DUP,Not copied from another line,The commentary is not identical to another line's in the same close (rule in code; not editable here),This is word for word the same as another line. Explain what drove this account specifically.,Copied text explains neither line,Each line names its own driver.,System,,n/a,Deterministic,Y
NEAR,Not a template,The commentary is not nearly identical to another line's; similarity threshold in Settings (rule in code),"This reads almost the same as another line. Make sure it explains this account's movement, not a shared template.",Templates hide the differences between accounts,Two salary lines that each name their own hires.,System,,Settings,Heuristic,Y
```

## The default settings

```csv
Setting,Value,Notes
Materiality ($),25000,A line is skipped when its $ variance is under this AND its % variance is under Materiality (%). Equal counts as material.
Materiality (%),0.1,When Prior is zero the % variance is undefined and the $ test decides alone.
Pinned model version,jev-1.13.0,Must match the service's pin. A mismatch is reported as an error; it never switches the model.
Health: never-fails rate,0.05,Prompt when a criterion's first-attempt fail rate is at or below this.
Health: always-fails rate,0.6,Prompt when a criterion's first-attempt fail rate is at or above this.
Health: near-threshold band,0.05,"Jev probabilities within this distance of the criterion's threshold count as near it. Jev's call-to-call spread is about 0.01, so 0.05 is close enough that a rerun could flip the verdict."
Health: near-threshold share,0.35,Prompt when at least this share of a criterion's probabilities sit near its threshold.
Health: flag-wrong weighted count,3,Prompt when weighted 'Flag was wrong' overrides on a criterion reach this.
Health: missed-issue weighted count,3,Prompt when weighted 'Missed an issue' overrides on a criterion (or 'not covered') reach this.
Health: reviewer override weight,2,Reviewer overrides count for more than preparer overrides.
Health: preparer override weight,1,A preparer saying a flag is wrong starts a conversation; it still counts.
Health: stale after (closes),2,Prompt when a criterion has not been reviewed in this many closes.
Health: window (closes),1,How many recent closes the Log-based signals read.
Minimum sample size,20,No prompt fires until a criterion has at least this many material lines behind it.
Near-duplicate similarity,0.85,Commentary at or above this similarity to another line is flagged as a likely near-duplicate.
Arithmetic tolerance ($),1000,"Cited amounts may miss the variance by the larger of this and Arithmetic tolerance (%), plus the rounding the citation implies."
Arithmetic tolerance (%),0.01,Share of the variance allowed as tolerance.
```

## The interview

### 1. Point at history

Ask the reviewer to paste or attach past flux commentary: a few closes, with each line's account and its prior and current amounts. A line without both amounts can't be labeled, because Jev reads the row's figures along with the commentary. If they'd rather not share any, start from the default criteria, skip step 2, and tell them that every criterion, the defaults included, will stay Uncalibrated until a sample is labeled. An uncalibrated criterion still runs, but its flags go to the reviewer as prompts instead of to preparers.

### 2. Label a sample

Show the default criteria as a short table (ID, Criterion, Passes when). Pick 15 to 20 lines that cover the range of the team's work: different accounts and sizes of movement, weak commentary as well as strong, first drafts if the files keep them, and no more than two lines from one account.

A criterion needs at least 6 lines that fail it and 6 that pass it before it can be calibrated, because calibration holds out a quarter of each label and needs two of each held out. A sample of only polished commentary leaves most criteria short of fails. After the first 15 to 20 lines, count each criterion's fails and passes. Where one is short, pick more lines likely to fill the gap (weak first drafts usually fail several criteria at once) and label those, until every criterion has 6 of each or the reviewer agrees which will stay Uncalibrated for now.

Show each line the way the sheet shows it: account, prior, current, change, commentary. Ask which criteria it fails, and for one sentence on why. Don't suggest marks: the labels are the reviewer's judgment, and Jev's thresholds are measured against them. Active criteria the reviewer leaves unmarked count as passes. Keep each reason in their words, and note any line that fails for a reason no criterion covers.

### 3. Ask what's missing

Ask what the reviewer wishes commentary did that it doesn't today. Get two or three concrete answers, each tied to a line they've seen. This keeps the criteria from simply writing down today's habits.

### 4. Draft and challenge

Go through the default criteria with the reviewer: keep, edit or retire each one. Then draft new criteria from the reasons in step 2 and the wishes in step 3. For each, write:

- ID: letters, digits, `-` or `_`, like C6.
- Criterion: a short name.
- Passes when: one condition the commentary meets, about what the text says ("Names the customer, vendor, contract or hire"). It can also say what doesn't count ("Fails when the cause is a category, even with an amount attached"). Spelling out the near misses is what made C1 reliable.
- The yes-or-no question that condition asks of a line ("Does the commentary name the customer, vendor, contract or hire?"). If you can't write one, reject the draft and tell the reviewer why.
- Feedback when failed: what a preparer reads when a line fails, in the reviewer's voice and drawn from the reasons they gave. Read it back; they approve it or rewrite it. It has to work for any line, so no placeholders and nothing about one particular line.
- Why it matters and Example of good. Quote the example from a sample line the reviewer marked as passing, when you can.

Reject these, and keep a list with the reason for each:

- conditions about truth, such as "the explanation is accurate", "ties to the GL" or "is reasonable";
- scores and ratings, such as "rates clarity from 1 to 5";
- anything that can't be asked as a yes or a no.

Push back on taste words such as "clear", "concise" or "sufficient detail": ask what a reader would see when it is done well, and write that instead.

ARITH, DUP and NEAR can have their Criterion, Feedback when failed, Why it matters and Example of good reworded, or be retired, but their Passes when describes the code and stays as shipped.

When a criterion is new or reworded after the sample was labeled, go back through the sample and ask which lines fail it. "Unmarked counts as a pass" only holds for criteria the reviewer had in front of them.

Four to seven criteria for Jev is plenty. Each one is another question on every line and another message a preparer may read.

### 5. Set materiality

The Coach skips a line when its $ variance is under Materiality ($) and its % variance is under Materiality (%). When the prior amount is zero, the $ test decides alone. The defaults are $25,000 and 10%. Ask the reviewer to confirm or change both.

### 6. Write the tabs

Write three CSV code blocks, each with its file name on the line above it, then the list of rejected drafts. Use standard CSV quoting: wrap a field in double quotes if it contains a comma, a double quote or a line break, and double any double quote inside it.

**criteria.csv** has exactly this header:

`ID,Criterion,Passes when,Feedback when failed,Why it matters,Example of good,Owner,Last reviewed,Threshold,Calibration status,Active (Y/N)`

- One row per criterion: the active criteria Jev grades first, then ARITH, DUP and NEAR, then retired criteria.
- A default criterion kept with the same Passes when keeps its starting Threshold and stays Uncalibrated. A new or reworded one has an empty Threshold and the Calibration status Uncalibrated. Calibration on the labeled sample sets both.
- ARITH, DUP and NEAR keep their Passes when, Threshold and Calibration status exactly as shipped.
- Retire a criterion by keeping its row with Active (Y/N) set to N, so the sheet's history records the decision.
- Owner: keep a default row's Owner; new criteria get the reviewer's role.
- Last reviewed: today's date as YYYY-MM-DD, on every row.

**settings.csv** is the default settings above with only the reviewer's changes, under the header `Setting,Value,Notes`. Write Materiality (%) as a fraction: 0.05 for 5%.

**labels.csv** has the header `text_id,account,comparison_basis,prior,current,commentary`, then one column per active criterion Jev grades (not ARITH, DUP or NEAR) in the order of criteria.csv, then `reason`.

- text_id: S01, S02 and so on.
- comparison_basis: MoM, unless the line compares against a quarter, a year, a budget or a forecast (QoQ, YoY, vs Budget, vs Forecast).
- prior and current: plain numbers, with no $ or commas, and a minus sign for negatives.
- commentary: exactly as written. Don't tidy it.
- One column per criterion: fail where the reviewer marked it, pass everywhere else.
- reason: the reviewer's sentence. Required on any line with a fail.

**Rejected drafts**: each rejected draft, and why.

Before you write them, check with the reviewer that:

- every default criterion is kept, edited or retired;
- every active criterion has a yes-or-no question you could ask of a line, and none asks whether an explanation is true;
- every Feedback when failed is text the reviewer approved;
- every line in the sample was marked against every active criterion;
- both materiality values are confirmed.
