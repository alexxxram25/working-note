# How a criterion becomes a Jev question

The question builder turns one criterion into one Jev noul (yes/no) question. Its version is logged on every Log row and is one of the four things a calibration depends on, alongside the model snapshot, the pass condition and the threshold. Change the builder and every criterion is uncalibrated until it is checked again.

## What is sent

- **State:** the row's account, comparison basis, prior, current and change, formatted as text, plus the commentary. No entity, period, Line ID, email or anything else.
- **Question:** a fixed instruction, plus the criterion's Passes when text as the `true` criterion and a fixed sentence as the `false` criterion.
- **Never sent:** the criterion's name, its feedback, its rationale or its example. Editing those cannot change a verdict, which is why a reviewer can rewrite feedback in their own words without recalibrating.

`backend/tests/fixtures/question_builder_golden.json` pins the exact output. A change to the builder that alters it fails the test until `QUESTION_BUILDER_VERSION` is bumped.

## qb-1.0.0 to qb-1.1.0

The first recording pass (qb-1.0.0) showed Jev reading pass conditions literally. C1 asks for a vendor, contract, hire, customer or event, and Jev scored "a large customer" and "new hires" at 0.93 to 0.97, because the words customer and hire appear in the condition. Jev's own documentation says the same thing: it answers the question you wrote, not the one you meant.

qb-1.1.0 asks for the reading a careful reviewer would give, without naming any criterion:

> Apply the standard strictly, as a careful reviewer would: generic or implied content does not satisfy a standard that asks for something specific.

The `false` criterion became "The commentary does not meet this standard, or meets it only vaguely or in part."

Both versions were run on all 112 labeled texts from Fernhill Data, the fictional company the tests use, against the same snapshot (`typesafe/jev-1.13-20260917`). Threshold-free ranking quality (AUC) and 4-fold cross-validated balanced accuracy:

| Criterion | AUC qb-1.0.0 | AUC qb-1.1.0 | CV balanced accuracy qb-1.0.0 | CV balanced accuracy qb-1.1.0 |
| --- | --- | --- | --- | --- |
| C1 | 0.884 | 0.938 | 0.778 | 0.871 |
| C2 | 0.982 | 0.987 | 0.913 | 0.893 |
| C3 | 0.998 | 0.998 | 0.988 | 0.983 |
| C4 | 0.993 | 0.987 | 0.919 | 0.925 |
| C5 | 0.978 | 0.987 | 0.911 | 0.923 |
| RESTATE | 0.976 | 0.967 | 0.886 | 0.947 |

C1, the criterion with the problem, improved clearly. The others moved within what one or two lines can change on a set this size. The raw probabilities for both versions are in `docs/evidence/question_builder_ab.json`.

## What the stricter wording did not fix

Jev still reads some conditions differently from a reviewer, and the data keeps those disagreements rather than hiding them:

- **C1**, under its original wording, still passed "one of our customers" and failed some specific but non-commercial drivers, such as an FX rate move on a named balance or employees crossing the Social Security wage base. That one was fixed in the pass condition rather than the question builder; see below.
- **C5** fails a single driver sized only by the total ("Down $5K because the fund's yield reset lower"), where a reviewer counts the driver as sized.
- **C4** sometimes doubts that a roll-forward schedule is something a reviewer can pull.

Where a reviewer disagrees with a flag, they mark it wrong with an Override, and Criteria Health raises those criteria for a look. That is the intended loop: the model makes bounded judgments, the reviewer owns the standard, and the disagreements are counted rather than argued one note at a time.

## C1's pass condition, tightened (2026-10-11)

The stricter instruction couldn't fix one thing on its own: C1's probability rose with any added detail. A vendor name, an amount or a date lifted a vague driver by 0.04 to 0.12, while a real driver gained 0.01 to 0.02. A reviewer fail could score 0.95 and a reviewer pass 0.93, so no threshold separated them. The fix had to be in the wording, so C1's "Passes when" now reads:

> Names the specific cause of the change, identified well enough that a reviewer could look it up: which customer, vendor, contract, hire or event, and what changed with it. Fails when the cause is a category ("timing", "growth", "higher volume"), even with an amount or dates attached, or when the only name given is where the cost was booked or who billed it.

The wording was drafted after seeing which shipped texts C1 got wrong, so its perfect score on the 112 shipped labels is partly in-sample. It shipped only after a blind check: 36 new texts, written and labeled before scoring, against rules fixed in advance. On them, balanced accuracy rose from 0.78 (old wording, threshold 0.91) to 0.97 (new wording, 0.47), and AUC from 0.92 to 0.997. The new wording kept all 18 texts labeled pass, including the FX, wage-base, board-approval and asset lines the old one failed. It caught 17 of the 18 labeled fail. Its one miss is the weak spot that remains: a vendor name plus an amount, with no cause, scored 0.59. Recalibrated on the shipped labels, C1's threshold is 0.43, and the blind result is the same there. Everything is in `docs/evidence/c1-blind-check/`.
