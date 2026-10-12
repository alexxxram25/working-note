"""Jev: TypeSafe's System One decision model, used only for bounded yes/no judgments.

Jev answers typed questions about a piece of state with probabilities. It
writes no text. Here it is asked one noul (yes/no) question per Jev-graded
criterion: does this commentary meet this pass condition? The answer is a
probability, compared against that criterion's own calibrated threshold.
Feedback never comes from Jev; it comes from the Criteria tab.
"""
