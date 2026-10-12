"""Criteria History: what changed in the Criteria tab, and what it did to calibration.

Each run sends the whole Criteria tab. When it differs from the last snapshot
the service stores, the service records a new snapshot and one history row per
criterion that changed, with a version number, the fields that changed (before
and after), and the calibration impact:

  - Passes when or Threshold changed: the criterion is uncalibrated until it
    is re-checked, because two of the four things a verdict depends on moved;
  - only Feedback, Criterion name, Why it matters, Example of good or Owner
    changed: no calibration impact, by construction (none of them reach Jev);
  - Active switched: retired or reactivated;
  - a new ID: version 1.

Every Log row carries the criterion version it was graded under, so anyone can
see why the feedback on a line changed from one month to the next.

Last reviewed and Calibration status are status, not content: re-confirming a
criterion updates Last reviewed without minting a new version.
"""

from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .hashing import content_hash
from .models import Criterion

CALIBRATION_FIELDS = ("Passes when", "Threshold")


class Change(NamedTuple):
    criterion_id: str
    version: int
    content: Dict[str, str]
    content_hash: str
    fields_changed: List[str]
    diff: Dict[str, Dict[str, str]]
    calibration_impact: str


def snapshot(criteria: Sequence[Criterion]) -> Tuple[str, List[Dict[str, str]]]:
    content = [c.sheet_content() for c in criteria]
    return content_hash(content), content


def impact(criterion: Criterion, fields: Sequence[str], is_new: bool) -> str:
    if is_new:
        if criterion.kind == "jev":
            return "new criterion: uncalibrated until it is checked on labeled lines"
        return "new criterion: rule in code, no calibration"
    touched = [f for f in fields if f in CALIBRATION_FIELDS]
    if touched and criterion.kind == "jev":
        return "uncalibrated: {0} changed".format(" and ".join(touched))
    if "Active (Y/N)" in fields:
        return "retired (inactive)" if not criterion.active else "reactivated"
    return "none: {0} only".format(", ".join(fields))


def changes(previous: Dict[str, Tuple[int, Dict[str, str]]], criteria: Sequence[Criterion]) -> List[Change]:
    """Compare the current tab with the latest version of each criterion.

    `previous` maps criterion ID to (version, content) as last recorded.
    Criteria that disappeared from the tab are not versioned here; removing a
    row is a reviewer action the next snapshot records as a whole.
    """
    out: List[Change] = []
    for criterion in criteria:
        content = criterion.sheet_content()
        digest = content_hash(content)
        before = previous.get(criterion.id)
        if before is None:
            out.append(Change(criterion.id, 1, content, digest, sorted(content), {},
                              impact(criterion, [], True)))
            continue
        version, old = before
        if content_hash(old) == digest:
            continue
        fields = [k for k in content if old.get(k) != content.get(k)]
        diff = {k: {"before": old.get(k, ""), "after": content.get(k, "")} for k in fields}
        out.append(Change(criterion.id, version + 1, content, digest, fields, diff,
                          impact(criterion, fields, False)))
    return out


def describe(change: Change) -> Dict[str, Any]:
    """A Criteria History tab row for this change."""
    if change.version == 1 and not change.diff:
        before, after = "", "initial version"
    else:
        before = "; ".join("{0}: {1}".format(k, v["before"]) for k, v in change.diff.items())
        after = "; ".join("{0}: {1}".format(k, v["after"]) for k, v in change.diff.items())
    return {
        "criterion_id": change.criterion_id,
        "version": change.version,
        "fields_changed": ", ".join(change.fields_changed) if change.diff else "all (new)",
        "before": before,
        "after": after,
        "calibration_impact": change.calibration_impact,
    }
