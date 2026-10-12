"""Choosing each criterion's threshold, and checking it on lines it never saw.

Five different questions will not behave alike under one global threshold, so
each Jev criterion is calibrated on its own:

  1. Every labeled text has a pass/fail label for this criterion and Jev's
     probability that it passes.
  2. A seeded, stratified 25% of texts is held out. The split is a hash of the
     seed and the text ID, so it is the same every time and independent of
     file order.
  3. On the other 75%, the threshold that maximizes balanced accuracy is
     chosen (mean of the pass-recall and fail-recall, so a criterion that
     rarely fails is not calibrated by always saying "pass"). Ties go to the
     middle of the best interval, which keeps the choice stable.
  4. The threshold is then checked on the held-out slice. The criterion is
     Calibrated only if held-out balanced accuracy and the number of held-out
     passes and fails clear the floors; otherwise it is Uncalibrated and its
     flags route to the reviewer as prompts.

A threshold is only as good as the labels behind it, so the service ships
none calibrated: each team calibrates on its own labeled lines from onboarding.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from .hashing import sha256_hex

GRID = [round(0.01 * i, 2) for i in range(1, 100)]


class Example(NamedTuple):
    text_id: str
    probability: float
    passes: bool  # ground-truth label


class Metrics(NamedTuple):
    n: int
    n_pass: int
    n_fail: int
    balanced_accuracy: float
    accuracy: float
    fail_recall: Optional[float]
    fail_precision: Optional[float]
    brier: float

    def as_dict(self) -> Dict[str, object]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self._asdict().items()}


class Calibration(NamedTuple):
    criterion_id: str
    threshold: float
    status: str  # Calibrated | Uncalibrated
    reason: str
    calibration: Metrics
    holdout: Metrics


class Floors(NamedTuple):
    min_balanced_accuracy: float = 0.75
    min_holdout_per_class: int = 2


def evaluate(examples: Sequence[Example], threshold: float) -> Metrics:
    tp = sum(1 for e in examples if e.passes and e.probability >= threshold)
    fn = sum(1 for e in examples if e.passes and e.probability < threshold)
    tn = sum(1 for e in examples if not e.passes and e.probability < threshold)
    fp = sum(1 for e in examples if not e.passes and e.probability >= threshold)
    n_pass, n_fail = tp + fn, tn + fp
    pass_recall = tp / n_pass if n_pass else None
    fail_recall = tn / n_fail if n_fail else None
    present = [r for r in (pass_recall, fail_recall) if r is not None]
    balanced = sum(present) / len(present) if present else 0.0
    flagged = tn + fn
    brier = (sum((e.probability - (1.0 if e.passes else 0.0)) ** 2 for e in examples) / len(examples)
             if examples else 0.0)
    return Metrics(
        n=len(examples), n_pass=n_pass, n_fail=n_fail, balanced_accuracy=balanced,
        accuracy=(tp + tn) / len(examples) if examples else 0.0,
        fail_recall=fail_recall, fail_precision=(tn / flagged) if flagged else None, brier=brier,
    )


def split(examples: Sequence[Example], seed: str, holdout: float = 0.25) -> Tuple[List[Example], List[Example]]:
    """Stratified by label; deterministic in the seed and text IDs alone."""
    train: List[Example] = []
    test: List[Example] = []
    for label in (True, False):
        group = sorted((e for e in examples if e.passes is label),
                       key=lambda e: sha256_hex(seed + "|" + e.text_id))
        cut = int(round(len(group) * holdout))
        if len(group) >= 2:
            cut = max(1, min(cut, len(group) - 1))
        test.extend(group[:cut])
        train.extend(group[cut:])
    return train, test


def choose_threshold(examples: Sequence[Example]) -> float:
    scored = [(evaluate(examples, t).balanced_accuracy, t) for t in GRID]
    best = max(score for score, _ in scored)
    ties = [t for score, t in scored if abs(score - best) < 1e-12]
    # Contiguous best run that contains the median tie, then its midpoint.
    middle = ties[len(ties) // 2]
    return round(middle, 2)


def calibrate(criterion_id: str, examples: Sequence[Example], seed: str,
              floors: Floors = Floors(), holdout: float = 0.25,
              fixed_threshold: Optional[float] = None) -> Calibration:
    train, test = split(examples, seed + "|" + criterion_id, holdout)
    threshold = fixed_threshold if fixed_threshold is not None else choose_threshold(train)
    cal, held = evaluate(train, threshold), evaluate(test, threshold)
    reasons = []
    if held.n_pass < floors.min_holdout_per_class or held.n_fail < floors.min_holdout_per_class:
        reasons.append("held-out slice has {0} pass and {1} fail labels; needs {2} of each".format(
            held.n_pass, held.n_fail, floors.min_holdout_per_class))
    if held.balanced_accuracy < floors.min_balanced_accuracy:
        reasons.append("held-out balanced accuracy {0:.2f} is under {1:.2f}".format(
            held.balanced_accuracy, floors.min_balanced_accuracy))
    status = "Uncalibrated" if reasons else "Calibrated"
    reason = "; ".join(reasons) if reasons else "held-out balanced accuracy {0:.2f} on {1} lines".format(
        held.balanced_accuracy, held.n)
    return Calibration(criterion_id, threshold, status, reason, cal, held)
