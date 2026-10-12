"""A labeled set: commentary texts, the row each explains, and pass/fail per criterion.

Calibration needs this and nothing else. It is the team's own sample from
onboarding (supporting/onboarding_prompt.md or the working-note-onboarding Skill), as
a CSV with these columns:

    text_id, account, comparison_basis, prior, current, commentary,
    <one column per criterion ID, each pass or fail>, reason

A team's labels are what make its thresholds real, which is why the service
ships no calibration of its own.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, NamedTuple, Sequence

from .models import LineIn

REQUIRED_COLUMNS = ["text_id", "account", "comparison_basis", "prior", "current", "commentary"]


class LabeledText(NamedTuple):
    text_id: str
    line: LineIn
    labels: Dict[str, str]  # criterion ID -> "pass" | "fail"


class LabeledSetError(ValueError):
    pass


def from_csv(path: Path, criterion_ids: Sequence[str]) -> List[LabeledText]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS + list(criterion_ids) if c not in header]
        if missing:
            raise LabeledSetError("{0} is missing columns: {1}".format(path, ", ".join(missing)))
        out: List[LabeledText] = []
        seen = set()
        for number, row in enumerate(reader, start=2):
            text_id = (row["text_id"] or "").strip()
            if not text_id or text_id in seen:
                raise LabeledSetError("{0}:{1}: text_id is blank or repeated".format(path, number))
            seen.add(text_id)
            labels: Dict[str, str] = {}
            for cid in criterion_ids:
                value = (row.get(cid) or "").strip().lower()
                # Onboarding rule: an active criterion left unmarked counts as a pass.
                labels[cid] = "fail" if value == "fail" else "pass"
                if value not in ("", "pass", "fail"):
                    raise LabeledSetError("{0}:{1}: {2} must be pass, fail or blank".format(path, number, cid))
            line = LineIn(line_id="TEAM|label|{0}|{1}".format(row.get("comparison_basis") or "MoM", number),
                          account=row["account"], basis=row.get("comparison_basis") or "MoM",
                          prior=row["prior"], current=row["current"], commentary=row["commentary"] or "")
            out.append(LabeledText(text_id, line, labels))
    if not out:
        raise LabeledSetError("{0} has no labeled lines".format(path))
    return out


def load(path: Path, criterion_ids: Sequence[str]) -> List[LabeledText]:
    return from_csv(path, criterion_ids)
