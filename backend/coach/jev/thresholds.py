"""Per-criterion thresholds and the calibrated / uncalibrated routing.

A Jev criterion passes when the probability that the commentary meets its pass
condition is at or above that criterion's own threshold. Below it:

  - calibrated criterion: the line fails that criterion, and the preparer sees
    the reviewer's feedback for it;
  - uncalibrated criterion: the flag becomes a prompt for the reviewer. It is
    not shown to the preparer as a failure, because nobody has checked that
    this criterion, worded this way, at this threshold, on this model, grades
    the way the team does.

Whether a criterion is calibrated is not read from the sheet. It is looked up
here, by fingerprint (served model snapshot, question-builder version, pass
condition, threshold), among the calibrations the service has recorded.
"""

from __future__ import annotations

from typing import Iterable, NamedTuple, Optional, Tuple

from ..models import Criterion
from .questions import calibration_fingerprint

DEFAULT_THRESHOLD = 0.5
CALIBRATED = "Calibrated"
UNCALIBRATED = "Uncalibrated"


class Grade(NamedTuple):
    result: str  # pass | fail | prompt
    threshold: float
    calibrated: bool


def grade(probability: float, threshold: Optional[float], calibrated: bool) -> Grade:
    cutoff = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    if probability >= cutoff:
        return Grade("pass", cutoff, calibrated)
    return Grade("fail" if calibrated else "prompt", cutoff, calibrated)


class CalibrationIndex:
    """Which (criterion, fingerprint) pairs currently stand as calibrated.

    Built from calibration records, most recent first per pair, so a later
    'uncalibrated' result for the same fingerprint (a recalibration that missed
    its floors) overrides an earlier 'calibrated' one.
    """

    def __init__(self, records: Iterable[Tuple[str, str, str]] = ()) -> None:
        self._status = {}
        for criterion_id, fingerprint, status in records:
            self._status[(criterion_id, fingerprint)] = status.lower() == "calibrated"

    def add(self, criterion_id: str, fingerprint: str, calibrated: bool) -> None:
        self._status[(criterion_id, fingerprint)] = calibrated

    def is_calibrated(self, criterion: Criterion, model_snapshot: str) -> Tuple[bool, str]:
        if criterion.threshold is None or not criterion.passes_when.strip():
            return False, ""
        fingerprint = calibration_fingerprint(model_snapshot, criterion.passes_when, criterion.threshold)
        return self._status.get((criterion.id, fingerprint), False), fingerprint

    def status_label(self, criterion: Criterion, model_snapshot: str) -> str:
        return CALIBRATED if self.is_calibrated(criterion, model_snapshot)[0] else UNCALIBRATED

    def __len__(self) -> int:
        return len(self._status)
