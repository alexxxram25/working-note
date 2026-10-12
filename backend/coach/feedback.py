"""Turning check outcomes into what the preparer sees.

The one rule: the Feedback cell contains only text a reviewer wrote in the
Criteria tab's "Feedback when failed" column, verbatim, labeled with the
criterion ID. Nothing here composes, paraphrases or fills in feedback. If a
reviewer left a criterion's feedback blank, the failed criterion still shows
in the Failed criteria column and the Feedback cell simply has nothing for it.

Certainty is carried by the labels, not by rewording the reviewer: flags from
Jev and from the near-duplicate heuristic are marked "(likely)" in Failed
criteria; deterministic failures are not. Probabilities and arithmetic detail
go in the Status note, where a curious preparer can look and nobody mistakes
them for feedback.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from .models import CheckOutcome, Criterion, FeedbackItem

PASSED = "passed"
NEEDS_REVISION = "needs_revision"
SKIPPED = "skipped_immaterial"
NEEDS_COMMENTARY = "needs_commentary"
REVIEWER_PROMPT = "reviewer_prompt"
ERROR = "error"

STATUS_LABELS = {
    PASSED: "Passed",
    NEEDS_REVISION: "Needs revision",
    SKIPPED: "Skipped: immaterial",
    NEEDS_COMMENTARY: "Needs commentary",
    REVIEWER_PROMPT: "Passed, reviewer prompt",
    ERROR: "Not graded",
}


def _ordered(outcomes: Sequence[CheckOutcome], criteria: Sequence[Criterion]) -> List[CheckOutcome]:
    position = {c.id: i for i, c in enumerate(criteria)}
    return sorted(outcomes, key=lambda o: (position.get(o.criterion_id, len(position)), o.criterion_id))


def assemble(
    outcomes: Sequence[CheckOutcome], criteria: Sequence[Criterion]
) -> Tuple[List[str], str, List[FeedbackItem], str, List[str]]:
    """Failed IDs, Failed criteria cell, feedback items, Feedback cell, prompt IDs."""
    by_id: Dict[str, Criterion] = {c.id: c for c in criteria}
    failed = [o for o in _ordered(outcomes, criteria) if o.result == "fail"]
    failed_ids = [o.criterion_id for o in failed]
    failed_cell = ", ".join(o.criterion_id + (" (likely)" if o.likely else "") for o in failed)
    items = []
    for outcome in failed:
        criterion = by_id.get(outcome.criterion_id)
        items.append(FeedbackItem(
            criterion_id=outcome.criterion_id,
            text=criterion.feedback if criterion else "",
            likely=outcome.likely,
        ))
    feedback_cell = "\n".join("{0}: {1}".format(i.criterion_id, i.text) for i in items if i.text.strip())
    prompts = [o.criterion_id for o in _ordered(outcomes, criteria) if o.result == "prompt"]
    return failed_ids, failed_cell, items, feedback_cell, prompts


def status_of(outcomes: Sequence[CheckOutcome], material: bool, blank: bool) -> str:
    if not material:
        return SKIPPED
    if blank:
        return NEEDS_COMMENTARY
    if any(o.result == "fail" for o in outcomes):
        return NEEDS_REVISION
    if any(o.result == "error" for o in outcomes):
        return ERROR
    if any(o.result == "prompt" for o in outcomes):
        return REVIEWER_PROMPT
    return PASSED


def _fmt_p(value: float) -> str:
    return "{0:.2f}".format(value)


def note(outcomes: Sequence[CheckOutcome], criteria: Sequence[Criterion], attempt: int) -> str:
    """The Status cell's note: facts and probabilities, one line per check."""
    lines = []
    for o in _ordered(outcomes, criteria):
        if o.tier == "jev" and o.probability is not None and o.threshold is not None:
            relation = ">=" if o.probability >= o.threshold else "<"
            verdict = {
                "pass": "likely met",
                "fail": "likely not met",
                "prompt": "flagged for the reviewer (criterion uncalibrated)",
            }.get(o.result, o.result)
            lines.append("{0}: p={1} {2} threshold {3}, {4}".format(
                o.criterion_id, _fmt_p(o.probability), relation, _fmt_p(o.threshold), verdict))
        elif o.result in ("abstain", "fail", "error", "blocked", "skipped", "missing") or o.detail:
            text = o.detail or o.result
            lines.append("{0}: {1}{2}".format(
                o.criterion_id, o.result, "" if text == o.result else " ({0})".format(text)))
    models = sorted({o.model_version for o in outcomes if o.model_version})
    if models:
        lines.append("Model {0}".format(", ".join(models)))
    if attempt:
        lines.append("Attempt {0}".format(attempt))
    return "\n".join(lines)
