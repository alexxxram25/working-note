"""Turning a criterion into a Jev question.

QUESTION_BUILDER_VERSION is part of every calibration fingerprint and every
Log row. Bump it whenever anything in this module changes what is sent to
Jev, because a different question can produce different probabilities and the
calibrated thresholds were measured on the old one. A golden test fails if the
output changes and the version does not.

What goes into a question, and what deliberately does not:
  - in: the criterion's Passes when text, and a fixed instruction;
  - out: the criterion's name, its feedback, its rationale and its example.
    Editing any of those cannot change the question, so it cannot change a
    verdict or invalidate a calibration. That is what lets a reviewer rewrite
    feedback in their own words without recalibrating.

The state is small and textual (TypeSafe's guidance: accuracy drops as
irrelevant state grows, and numbers read better as formatted text). Entity,
period and Line ID are not sent; they do not help judge commentary.

Each question sets `criteria` with both `true` and `false`. OpenRouter's
Decisions API requires both for a noul, and TypeSafe recommends criteria when
the boundary is subtle, which every pass condition here is.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from ..hashing import canonical_json, normalize_commentary, sha256_hex
from ..models import Criterion, LineIn

QUESTION_BUILDER_VERSION = "qb-1.1.0"

# qb-1.1.0 asks for the reading a careful reviewer would give. Jev answers the
# question as written, and qb-1.0.0 let generic content ("a large customer",
# "new hires") satisfy pass conditions that ask for something specific. On the
# labeled set the stricter wording raised C1's AUC from 0.884 to 0.938 and moved
# no other criterion outside noise (docs/question-builder.md). The instruction
# stays criterion-neutral: nothing here names a particular criterion.
INSTRUCTIONS = (
    "Read `commentary`, which a preparer wrote to explain the change shown in `row`. "
    "Does the commentary meet the standard described in the criteria? Apply the standard strictly, "
    "as a careful reviewer would: generic or implied content does not satisfy a standard that asks for "
    "something specific. Judge only what the commentary says, not whether it is true."
)
FALSE_CRITERION = "The commentary does not meet this standard, or meets it only vaguely or in part."

_BASIS_WORDS = {
    "mom": "month over month",
    "qoq": "quarter over quarter",
    "yoy": "year over year",
    "vs budget": "against budget",
    "bva": "against budget",
    "vs forecast": "against forecast",
}


def _usd(value: Optional[float], signed: bool = False) -> str:
    if value is None:
        return "not given"
    text = "${0:,.0f}".format(abs(value))
    if signed:
        return ("+" if value >= 0 else "-") + text
    return ("-" if value < 0 else "") + text


def build_state(line: LineIn, variance_usd: float, variance_pct: Optional[float]) -> Dict[str, Any]:
    if variance_pct is None:
        change = "{0} (prior was zero)".format(_usd(variance_usd, signed=True))
    else:
        change = "{0} ({1}{2:.1%})".format(_usd(variance_usd, signed=True),
                                           "+" if variance_pct >= 0 else "", variance_pct)
    basis = (line.basis or "").strip()
    return {
        "row": {
            "account": (line.account or "").strip(),
            "comparison": _BASIS_WORDS.get(basis.lower(), basis),
            "prior": _usd(line.prior),
            "current": _usd(line.current),
            "change": change,
        },
        "commentary": normalize_commentary(line.commentary),
    }


def build_question(criterion: Criterion) -> Dict[str, Any]:
    return {
        "type": "noul",
        "instructions": INSTRUCTIONS,
        "criteria": {"true": normalize_commentary(criterion.passes_when), "false": FALSE_CRITERION},
    }


def build_questions(criteria: Iterable[Criterion]) -> Dict[str, Dict[str, Any]]:
    return {c.id: build_question(c) for c in criteria}


def question_fingerprint(request_model: str, state: Dict[str, Any], question: Dict[str, Any]) -> str:
    """Identity of one question as Jev would see it: model, state and question."""
    return "q:" + sha256_hex(canonical_json({"model": request_model, "state": state, "question": question}))


def passes_when_hash(passes_when: str) -> str:
    return "sha256:" + sha256_hex(normalize_commentary(passes_when))


def calibration_fingerprint(model_snapshot: str, passes_when: str, threshold: Optional[float],
                            builder_version: str = QUESTION_BUILDER_VERSION) -> str:
    """The four things a verdict depends on. Change any one and calibration lapses."""
    return "cal:" + sha256_hex(canonical_json({
        "model_snapshot": model_snapshot,
        "question_builder_version": builder_version,
        "passes_when": normalize_commentary(passes_when),
        "threshold": None if threshold is None else "{0:.3f}".format(threshold),
    }))
