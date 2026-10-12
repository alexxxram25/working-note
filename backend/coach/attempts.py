"""Attempt numbering: what makes a rerun a second attempt.

A second attempt counts only when the same Line ID comes back with a changed
commentary hash after failing a check. So:

    no earlier attempt                      -> 1
    commentary unchanged since last attempt  -> same number (a rerun is not an attempt)
    changed, and the last attempt failed     -> number + 1
    changed, but the last attempt passed     -> same number (polishing is not a retry)
    blank commentary                         -> 0, or the previous number (nothing was submitted)

"Last attempt" means the last one whose results were delivered to the sheet.
If the preparer edited a line while a run was in flight, the script reports
it as stale and its results are never shown; an attempt the preparer never
saw cannot be the one they were responding to. A new close period is a new
Line ID, so it starts again at 1.
"""

from __future__ import annotations

from typing import NamedTuple, Optional


class PriorAttempt(NamedTuple):
    attempt_number: int
    commentary_hash: str
    failed: bool


def next_attempt(prior: Optional[PriorAttempt], commentary_hash: str, blank: bool) -> int:
    if blank:
        return prior.attempt_number if prior else 0
    if prior is None or prior.attempt_number == 0:
        return 1
    if commentary_hash == prior.commentary_hash:
        return prior.attempt_number
    if prior.failed:
        return prior.attempt_number + 1
    return prior.attempt_number


def is_second_attempt(prior: Optional[PriorAttempt], attempt_number: int) -> bool:
    return prior is not None and attempt_number > prior.attempt_number >= 1
