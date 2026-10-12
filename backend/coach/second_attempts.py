"""The second-attempts view: which criteria needed fewer second attempts over time.

A line counts toward a criterion's second attempts in a close when it failed
that criterion on attempt k and came back as attempt k + 1, which only happens
after its commentary changed. A rerun with unchanged commentary is the same
attempt and does not count; a new close period is a new Line ID.

The rate's denominator is the number of lines that got a first-attempt verdict
on that criterion in that close, so a close with more material lines does not
look worse just for being bigger.

This measures the team, not people: there is no per-preparer view, and the
data could not support one honestly anyway.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import Criterion

CAVEAT = ("This counts revisions, not learning: software can't tell whether a preparer understood the feedback "
          "or reworded until it passed.")
VERDICTS = ("pass", "fail", "prompt")


def compute(rows: Iterable[Dict[str, Any]], criteria: Sequence[Criterion],
            closes: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    failed_at: Dict[Tuple[str, str, int], Set[str]] = defaultdict(set)
    attempts: Dict[Tuple[str, str], Set[int]] = defaultdict(set)
    first_verdicts: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    seen_closes: Set[str] = set()
    for row in rows:
        attempt = row.get("attempt_number") or 0
        if attempt < 1:
            continue
        close, line_id, criterion_id = row["close_period"], row["line_id"], row["criterion_id"]
        seen_closes.add(close)
        attempts[(close, line_id)].add(attempt)
        if row["result"] == "fail":
            failed_at[(close, line_id, attempt)].add(criterion_id)
        if attempt == 1 and row["result"] in VERDICTS:
            first_verdicts[(close, criterion_id)].add(line_id)

    second: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    any_second: Dict[str, Set[str]] = defaultdict(set)
    for (close, line_id, attempt), failed in failed_at.items():
        if attempt + 1 in attempts[(close, line_id)]:
            for criterion_id in failed:
                second[(close, criterion_id)].add(line_id)
            any_second[close].add(line_id)

    ordered_closes = list(closes) if closes else sorted(seen_closes)
    table = []
    for criterion in criteria:
        counts = {c: len(second.get((c, criterion.id), ())) for c in ordered_closes}
        denominators = {c: len(first_verdicts.get((c, criterion.id), ())) for c in ordered_closes}
        rates = {c: (counts[c] / denominators[c] if denominators[c] else None) for c in ordered_closes}
        table.append({"criterion_id": criterion.id, "criterion": criterion.name, "counts": counts,
                      "first_attempt_lines": denominators, "rates": rates})
    totals = {c: len(any_second.get(c, ())) for c in ordered_closes}
    return {"closes": ordered_closes, "rows": table, "totals": totals, "caveat": CAVEAT,
            "falling": all(a > b for a, b in zip([totals[c] for c in ordered_closes],
                                                  [totals[c] for c in ordered_closes][1:]))}
