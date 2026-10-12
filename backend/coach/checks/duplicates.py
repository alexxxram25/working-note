"""Exact and near-duplicate commentary within one close.

Exact duplicate (deterministic): two material lines in the same close whose
commentary is identical once case and whitespace are ignored. Copied text
explains neither line, so it fails before any model call.

Near duplicate (heuristic): commentary that reads almost the same as another
line's once the amounts are masked, which is what a template looks like after
someone changes only the numbers. Similarity is difflib's ratio, threshold from
Settings. It is reported as likely, not certain, and it does not stop Jev from
grading the line: a template can still name a real driver.

Only material lines with commentary are compared; immaterial lines are
skipped entirely, so two "Flat." lines below materiality are not duplicates.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Dict, Iterable, List, NamedTuple, Tuple

_WHITESPACE = re.compile(r"\s+")
_AMOUNT = re.compile(r"(?:US\$|\$|USD\s?)\s?\d[\d,]*(?:\.\d+)?\s?(?:thousand|million|billion|mm|mn|bn|k|m|b)?\b",
                     re.IGNORECASE)
_DIGITS = re.compile(r"\d[\d,]*(?:\.\d+)?")


class DuplicateResult(NamedTuple):
    exact_with: Tuple[str, ...] = ()
    near_with: Tuple[Tuple[str, float], ...] = ()

    @property
    def exact(self) -> bool:
        return bool(self.exact_with)

    @property
    def near(self) -> bool:
        return bool(self.near_with)


def normalize_exact(text: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", text or "")).strip().lower()


def normalize_near(text: str) -> str:
    masked = _AMOUNT.sub("<amt>", normalize_exact(text))
    return _DIGITS.sub("#", masked)


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def find(entries: Iterable[Tuple[str, str]], near_threshold: float) -> Dict[str, DuplicateResult]:
    """Compare every pair of (line_id, commentary). Blank commentary is ignored."""
    items = [(lid, text) for lid, text in entries if (text or "").strip()]
    exact_keys = {lid: normalize_exact(text) for lid, text in items}
    near_keys = {lid: normalize_near(text) for lid, text in items}

    groups: Dict[str, List[str]] = {}
    for lid, key in exact_keys.items():
        groups.setdefault(key, []).append(lid)

    exact: Dict[str, List[str]] = {lid: [] for lid, _ in items}
    for members in groups.values():
        if len(members) > 1:
            for lid in members:
                exact[lid] = [other for other in members if other != lid]

    near: Dict[str, List[Tuple[str, float]]] = {lid: [] for lid, _ in items}
    ids = [lid for lid, _ in items]
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if exact_keys[left] == exact_keys[right]:
                continue
            a, b = near_keys[left], near_keys[right]
            matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
            # Cheap upper bounds first; most pairs are nowhere near the threshold.
            if matcher.real_quick_ratio() < near_threshold or matcher.quick_ratio() < near_threshold:
                continue
            score = matcher.ratio()
            if score >= near_threshold:
                near[left].append((right, round(score, 3)))
                near[right].append((left, round(score, 3)))

    return {
        lid: DuplicateResult(tuple(exact[lid]), tuple(sorted(near[lid], key=lambda p: -p[1])))
        for lid in ids
    }
