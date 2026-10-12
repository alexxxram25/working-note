"""Criteria Health: review prompts at the end of each close, never diagnoses.

A criterion that rarely fails may be mastered, irrelevant, vague or badly
evaluated, and only the reviewer can tell which. So each signal raises a
prompt with the evidence behind it, and the reviewer decides: edit, retire or
keep. Keeping a criterion re-confirms its wording; it does not validate its
model performance.

Signals (thresholds in Settings):
  never_fails     first-attempt fail rate at or below the never-fails rate (Log)
  always_fails    first-attempt fail rate at or above the always-fails rate (Log)
  near_threshold  enough Jev probabilities sit within the band of the
                  criterion's threshold (Log)
  flag_wrong      weighted "Flag was wrong" overrides reach the minimum
  missed_issue    weighted "Missed an issue" overrides on this criterion
  not_covered     weighted "Missed an issue: not covered" overrides, across
                  all criteria: a standard no criterion covers
  stale           not reviewed in the last N closes (Last reviewed)

No prompt fires until a criterion has the minimum number of material lines
behind it. Reviewer overrides count for more than preparer overrides. A
"flag was wrong" on a line where that criterion never failed is ignored.

The three Log-rate signals apply to Jev-graded criteria only. The arithmetic
and duplicate checks are guards: rarely firing is their job, so "almost never
fails" says nothing about them a reviewer could act on. Overrides and
staleness still apply to every row.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import second_attempts
from .models import COMMENTARY_ID, MATERIALITY_ID, Criterion, Settings
from .store.base import Store

SUGGESTIONS = {
    "never_fails": "Team has learned it (retire it) or it's too loose",
    "always_fails": "Too strict, unclear, or not being taught",
    "near_threshold": "Criterion is vague; rewrite as a sharper condition",
    "flag_wrong": "Criterion is miscalibrated for this team",
    "missed_issue": "Criterion is passing commentary the reviewer still marks up",
    "not_covered": "A standard exists that no criterion covers",
    "stale": "Nobody is maintaining it",
}
NOT_COVERED_ID = "NOT COVERED"
_PERIOD = re.compile(r"^(\d{4})-(\d{2})$")


def _close_starts(period: str) -> Optional[dt.date]:
    """A monthly close for period YYYY-MM happens in the month after it."""
    match = _PERIOD.match(period or "")
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1)


def _window_label(window: Sequence[str]) -> str:
    if not window:
        return ""
    return window[0] if len(window) == 1 else "{0} to {1}".format(window[0], window[-1])


def _weight(role: str, settings: Settings) -> float:
    return settings.health_reviewer_weight if role == "reviewer" else settings.health_preparer_weight


def health_report(store: Store, sheet_id: str, criteria: Sequence[Criterion], settings: Settings,
                  today: Optional[dt.date] = None) -> Dict[str, Any]:
    periods = store.close_periods(sheet_id)
    window = periods[-settings.health_window_closes:] if periods and settings.health_window_closes else periods
    label = _window_label(window)
    rows = store.log_rows(sheet_id, window) if window else []
    overrides = store.overrides(sheet_id, window) if window else []

    material: Set[str] = set()
    first: Dict[str, Dict[str, str]] = defaultdict(dict)  # criterion -> line -> first verdict
    probabilities: Dict[str, Dict[Tuple[str, str], Tuple[float, float]]] = defaultdict(dict)
    for row in rows:
        if row["criterion_id"] not in (MATERIALITY_ID,):
            material.add(row["line_id"])
        if row["criterion_id"] in (MATERIALITY_ID, COMMENTARY_ID):
            continue
        if row.get("attempt_number") == 1 and row["result"] in ("pass", "fail", "prompt"):
            first[row["criterion_id"]].setdefault(row["line_id"], row["result"])
        if row.get("jev_probability") is not None and row.get("threshold") is not None:
            key = (row["line_id"], row["commentary_hash"])
            probabilities[row["criterion_id"]][key] = (float(row["jev_probability"]), float(row["threshold"]))

    flag_wrong: Dict[str, float] = defaultdict(float)
    missed: Dict[str, float] = defaultdict(float)
    not_covered = 0.0
    not_covered_notes: List[str] = []
    for item in overrides:
        weight = _weight(item["role"], settings)
        if item["kind"] == "flag_wrong" and item.get("criterion_id") and item.get("matched_flag") is not False:
            flag_wrong[item["criterion_id"]] += weight
        elif item["kind"] == "missed_issue":
            if item.get("not_covered"):
                not_covered += weight
                if item.get("note"):
                    not_covered_notes.append(item["note"])
            elif item.get("criterion_id"):
                missed[item["criterion_id"]] += weight

    stale_cutoff = None
    if len(periods) >= settings.health_stale_closes > 0:
        stale_cutoff = _close_starts(periods[-settings.health_stale_closes])

    prompts: List[Dict[str, Any]] = []
    gated: List[Dict[str, Any]] = []

    def prompt(criterion_id: str, name: str, signal: str, evidence: Dict[str, Any], text: str) -> None:
        prompts.append({"criterion_id": criterion_id, "criterion": name, "signal": signal,
                        "suggestion": SUGGESTIONS[signal], "evidence": dict(evidence, text=text),
                        "close_window": label})

    for criterion in criteria:
        if not criterion.active:
            continue
        verdicts = first.get(criterion.id, {})
        n = len(verdicts)
        if n < settings.min_sample_size:
            gated.append({"criterion_id": criterion.id, "material_lines": n, "needed": settings.min_sample_size})
            continue
        fails = sum(1 for v in verdicts.values() if v in ("fail", "prompt"))
        rate = fails / n
        graded = criterion.kind == "jev"
        if graded and rate <= settings.health_never_fails_rate:
            prompt(criterion.id, criterion.name, "never_fails", {"fails": fails, "lines": n, "rate": round(rate, 3)},
                   "failed {0} of {1} first attempts ({2:.0%}) in {3}".format(fails, n, rate, label))
        if graded and rate >= settings.health_always_fails_rate:
            prompt(criterion.id, criterion.name, "always_fails", {"fails": fails, "lines": n, "rate": round(rate, 3)},
                   "failed {0} of {1} first attempts ({2:.0%}) in {3}".format(fails, n, rate, label))
        probs = list(probabilities.get(criterion.id, {}).values())
        if probs and criterion.kind == "jev":
            near = sum(1 for p, t in probs if abs(p - t) <= settings.health_near_band)
            share = near / len(probs)
            if share >= settings.health_near_share:
                prompt(criterion.id, criterion.name, "near_threshold",
                       {"near": near, "probabilities": len(probs), "share": round(share, 3),
                        "band": settings.health_near_band},
                       "{0} of {1} probabilities ({2:.0%}) within {3:.2f} of the threshold".format(
                           near, len(probs), share, settings.health_near_band))
        if flag_wrong.get(criterion.id, 0) >= settings.health_flag_wrong_min:
            prompt(criterion.id, criterion.name, "flag_wrong", {"weighted": flag_wrong[criterion.id]},
                   "'Flag was wrong' overrides weigh {0:g} (reviewer x{1:g}, preparer x{2:g})".format(
                       flag_wrong[criterion.id], settings.health_reviewer_weight, settings.health_preparer_weight))
        if missed.get(criterion.id, 0) >= settings.health_missed_min:
            prompt(criterion.id, criterion.name, "missed_issue", {"weighted": missed[criterion.id]},
                   "'Missed an issue' overrides on this criterion weigh {0:g}".format(missed[criterion.id]))
        if stale_cutoff is not None:
            reviewed = None
            try:
                reviewed = dt.date.fromisoformat(criterion.last_reviewed) if criterion.last_reviewed else None
            except ValueError:
                reviewed = None
            if reviewed is None or reviewed < stale_cutoff:
                prompt(criterion.id, criterion.name, "stale",
                       {"last_reviewed": criterion.last_reviewed, "cutoff": stale_cutoff.isoformat(),
                        "closes": settings.health_stale_closes},
                       "last reviewed {0}; not reviewed in the last {1} closes".format(
                           criterion.last_reviewed or "never", settings.health_stale_closes))

    if not_covered >= settings.health_missed_min and len(material) >= settings.min_sample_size:
        prompt(NOT_COVERED_ID, "No criterion covers this", "not_covered",
               {"weighted": not_covered, "notes": not_covered_notes[:10]},
               "'Missed an issue: not covered' overrides weigh {0:g}: {1}".format(
                   not_covered, " | ".join(not_covered_notes[:3])))

    all_rows = store.log_rows(sheet_id)
    return {
        "close_window": label,
        "material_lines": len(material),
        "prompts": prompts,
        "gated": gated,
        "second_attempts": second_attempts.compute(all_rows, [c for c in criteria if c.active], periods),
    }
