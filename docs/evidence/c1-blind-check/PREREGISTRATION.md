# C1 blind check: rules fixed before any blind text is scored

Written 2026-10-11 by the main Working Note session, which did not draft candidate A.
Nothing in this folder had been sent to Jev when this file was written.

## What is compared
- Today's C1 "Passes when" (the shipped wording) at its shipped threshold, 0.91.
- Candidate A (criteria_c1_a.csv in the C1 session's notes) at 0.47: calibrate.py's threshold for A
  on the 112 shipped labels plus the 4 representative bench texts (the set the memo would ship).
  Results at A's other thresholds (0.43, 0.70) are reported for sensitivity only; the decision uses 0.47.

## The blind set
36 new commentary texts (blind_labels.csv), written and labeled before scoring, 18 pass and 18 fail.
Label rule, the reviewer's test from C1's own feedback ("Who or what, specifically?"): does the
commentary identify the cause of the movement well enough that a reviewer could go to one specific
thing and check it (a named customer, contract, hire, event, approval, or a vendor that itself
changed something)? Categories (growth, timing, volume, activity, seasonality, "true-up") fail even
with amounts or dates. Naming only the vendor that billed the account, or where it was booked, or a
report to look in, does not make a category specific. Specific non-commercial drivers (an FX move on
a named balance, a statutory limit, a board approval, an asset placed in service) pass. A line with
one specific driver and a vague remainder passes C1; the remainder is C5's concern.

## Decision: ship A only if all three hold on the blind set
R1. Balanced accuracy: A at 0.47 is at least 0.05 above today's C1 at 0.91.
R2. AUC: A's is at least today's.
R3. A keeps true passes: at 0.47 it passes at least 80% of the texts labeled pass.
Then, before A reaches the demo (R4, checked in the integration): under A the demo line's first draft
still fails C1 and its revision passes, and second attempts still fall July > August > September.
If R1, R2 or R3 fails, today's C1 stays and the result is reported as is.
