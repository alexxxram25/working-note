"""The arithmetic check: do the amounts cited for the drivers add up to the variance?

Deterministic once the text is parsed, and conservative about parsing. When
the amounts cannot be read cleanly the check abstains rather than guessing:
an abstention is not a failure, gives the preparer no feedback, and is logged
so the reviewer can see how often it happens.

What is parsed
    Only currency-marked amounts: $, US$ or USD, optionally scaled with
    k/thousand, m/MM/million or bn/billion. Bare numbers (headcount, dates,
    invoice numbers, "Q3") are never amounts.

When it abstains
    - an amount in another currency (GBP, EUR, a pound or euro sign);
    - a range ("$40K to $50K", "between $1M and $2M");
    - a percentage other than the row's own variance %;
    - offset or netting language ("offset", "net of", "partly reversed");
    - an explicit negative amount;
    - one amount compared against another ("$27K versus $82K");
    - a driver whose direction opposes the variance ("added $12K" on a decrease);
    - the same amount cited for two drivers (which is which?);
    - an amount larger than the variance that is not a balance (context, such
      as a coverage limit or a capitalized base, not a driver);
    - no driver amounts at all, which is C5's concern, not this check's.

How it reconciles
    One cited amount that matches the variance is read as the restated total
    and set aside. Per-unit amounts ("$14K each", "cost per lead above $900")
    are context. The rest are drivers. The check passes when the drivers sum
    to |variance| within max(Settings $, Settings % x |variance|) plus the
    rounding each citation implies: "$1.2M" is only precise to $50K.

    An amount equal to the row's prior or current balance may be a balance
    citation ("returns to the normal $120K accrual") rather than a driver. The
    drivers are tried as cited first and then with balance citations set
    aside; if either reading reconciles cleanly, the check passes. Both
    readings are explicit and reported in the detail.

What it does not do
    Decide whether any amount is true. A commentary can reconcile perfectly
    and still be wrong about the business; that stays with the reviewer.
"""

from __future__ import annotations

import re
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple

PASS = "pass"
FAIL = "fail"
ABSTAIN = "abstain"

_CURRENCY = r"(?:US\$|\$|USD\s?)"
_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?"
_SCALE = r"(?:\s?(thousand|million|billion|MM|mm|mn|bn|k|K|m|M|b|B)\b)?"
_AMOUNT = re.compile(_CURRENCY + r"\s?" + _NUMBER + _SCALE)

_SCALES = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
}

_FOREIGN = re.compile(r"[£€]\s?\d|\b(?:GBP|EUR)\s?\d", re.IGNORECASE)
_RANGE = re.compile(
    _CURRENCY + r"\s?\d[\d,]*(?:\.\d+)?\s?(?:k|K|m|M|MM|mm|bn|b|B|thousand|million|billion)?"
    r"\s*(?:-|–|—|to)\s*" + _CURRENCY + r"?\s?\d",
)
_BETWEEN = re.compile(r"\bbetween\s+" + _CURRENCY + r"\s?\d[^.;]*?\band\s+" + _CURRENCY + r"?\s?\d",
                      re.IGNORECASE)
_PERCENT = re.compile(r"(\d+(?:\.(\d+))?)\s?(?:%|percent\b|per cent\b)", re.IGNORECASE)
_OFFSET = re.compile(
    r"\b(?:offset|offsets|offsetting|net of|netted|netting|partly reversed|partially reversed)\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(r"(?:(?<![\w$])-|−|\bminus\s)\s?" + _CURRENCY + r"|\$\s?-\s?\d", re.IGNORECASE)
_COMPARATORS = re.compile(r"\b(?:vs\.?|versus|compared\s+(?:to|with)|than|against)\b", re.IGNORECASE)
_SENTENCE = re.compile(r"[.;:!?](?:\s|$)")
_VS_DOT = re.compile(r"\bvs\.", re.IGNORECASE)
# Hyphenated compounds stay whole: "true-up" is not "up", "one-time" is not "time".
_WORD = re.compile(r"[A-Za-z']+(?:-[A-Za-z']+)*")

_UP = {
    "up", "rose", "risen", "rise", "rises", "higher", "increase", "increased", "increases",
    "increasing", "grew", "grow", "grows", "growing", "added", "adding", "adds", "add", "more",
    "jump", "jumped",
}
_DOWN = {
    "down", "fell", "fall", "falls", "fallen", "lower", "lowered", "decrease", "decreased",
    "decreases", "decreasing", "dropped", "drop", "drops", "declined", "decline", "declines",
    "reduced", "reduce", "reduction", "cut", "cuts", "cutting", "saved", "saving", "savings",
    "less", "lighter",
}
_PER_UNIT_AFTER = re.compile(
    r"^\W{0,3}(?:\w+\W+){0,3}?(?:each|apiece|per\s+(?!month\b|mo\b|period\b)\w+|an?\s+(?:hour|seat|user|head|lead)\b|/(?:hr|seat|user)\b)",
    re.IGNORECASE,
)
_PER_UNIT_BEFORE = re.compile(r"\bper\s+(?!month\b|mo\b|period\b)\w+(?:\W+\w+){0,2}\W*$", re.IGNORECASE)
_WINDOW_WORDS = 6


class Amount(NamedTuple):
    value: float
    precision: float
    start: int
    end: int
    raw: str
    per_unit: bool
    sign: int  # +1 up, -1 down, 0 no direction word nearby


class ArithResult(NamedTuple):
    result: str
    reason: str
    detail: str
    cited: Tuple[float, ...] = ()
    drivers: Tuple[float, ...] = ()
    variance: float = 0.0
    tolerance: float = 0.0


def _money(value: float) -> str:
    return "${0:,.0f}".format(value)


def _sentence_bounds(text: str, index: int) -> Tuple[int, int]:
    start = 0
    for match in _SENTENCE.finditer(text):
        if match.end() <= index:
            start = match.end()
        elif match.start() >= index:
            return start, match.start()
    return start, len(text)


def _direction(text: str, start: int, end: int) -> int:
    """Direction of the nearest direction word within the amount's sentence."""
    lo, hi = _sentence_bounds(text, start)
    before = _WORD.findall(text[lo:start])[-_WINDOW_WORDS:]
    after = _WORD.findall(text[end:hi])[:_WINDOW_WORDS]
    candidates = []
    for distance, word in enumerate(reversed(before)):
        candidates.append((distance, word.lower()))
    for distance, word in enumerate(after):
        candidates.append((distance, word.lower()))
    for _, word in sorted(candidates, key=lambda c: c[0]):
        if word in _UP:
            return 1
        if word in _DOWN:
            return -1
    return 0


def parse_amounts(text: str) -> List[Amount]:
    amounts = []
    for match in _AMOUNT.finditer(text or ""):
        whole, decimals, scale = match.group(1), match.group(2), match.group(3)
        multiplier = _SCALES.get((scale or "").lower(), 1.0)
        digits = whole.replace(",", "")
        value = float(digits + ("." + decimals if decimals else "")) * multiplier
        places = len(decimals) if decimals else 0
        precision = 0.5 * (10 ** -places) * multiplier
        after = text[match.end():match.end() + 40]
        before = text[max(0, match.start() - 30):match.start()]
        per_unit = bool(_PER_UNIT_AFTER.match(after) or _PER_UNIT_BEFORE.search(before))
        amounts.append(Amount(
            value=value, precision=precision, start=match.start(), end=match.end(),
            raw=match.group(0).strip(), per_unit=per_unit,
            sign=_direction(text, match.start(), match.end()),
        ))
    return amounts


def _abstain(reason: str, detail: str, variance: float, cited: Iterable[float] = ()) -> ArithResult:
    return ArithResult(ABSTAIN, "abstain:" + reason, detail, tuple(cited), (), variance, 0.0)


def _percent_matches(value_text: str, decimals: Optional[str], variance_pct: Optional[float]) -> bool:
    if variance_pct is None:
        return False
    cited = float(value_text)
    places = len(decimals) if decimals else 0
    slack = 0.5 * (10 ** -places)
    return abs(cited - abs(variance_pct) * 100.0) <= slack + 1e-9


def _compared(text: str, amounts: Sequence[Amount]) -> bool:
    for left, right in zip(amounts, amounts[1:]):
        # "vs." ends in a period but does not end the sentence.
        between = _VS_DOT.sub("vs", text[left.end:right.start])
        if len(between) <= 60 and not _SENTENCE.search(between) and _COMPARATORS.search(between):
            return True
    return False


def _close(a: float, b: float, slack: float) -> bool:
    return abs(a - b) <= slack + 1e-6


def _reconcile(candidates: List[Amount], target: float, base_tol: float):
    """Try one reading of the amounts. Returns (status, drivers, tolerance, why)."""
    if not candidates:
        return "empty", [], base_tol, "no driver amounts"
    remaining = list(candidates)
    total = None
    if len(remaining) > 1:
        for amount in remaining:
            if _close(amount.value, target, base_tol + amount.precision):
                total = amount
                break
        if total is not None:
            remaining.remove(total)
    if not remaining:
        return "clean", [], base_tol + total.precision, "cites the variance only"
    tolerance = base_tol + sum(a.precision for a in remaining)
    values = [a.value for a in remaining]
    for i, first in enumerate(remaining):
        for second in remaining[i + 1:]:
            if _close(first.value, second.value, first.precision + second.precision):
                return "ambiguous", values, tolerance, "the same amount is cited for two drivers"
    oversize = [a for a in remaining if a.value > target + tolerance]
    if oversize:
        return "oversize", values, tolerance, "{0} is larger than the variance".format(oversize[0].raw)
    if _close(sum(values), target, tolerance):
        return "clean", values, tolerance, "reconciled"
    return "mismatch", values, tolerance, "does not reconcile"


def check(
    text: str,
    variance_usd: float,
    variance_pct: Optional[float],
    prior: Optional[float],
    current: Optional[float],
    tolerance_usd: float = 1000.0,
    tolerance_pct: float = 0.01,
) -> ArithResult:
    target = abs(float(variance_usd))
    text = text or ""

    if _FOREIGN.search(text):
        return _abstain("currency", "Cites an amount in another currency.", target)
    if _RANGE.search(text) or _BETWEEN.search(text):
        return _abstain("range", "Cites a range, so there is no single amount to test.", target)
    for match in _PERCENT.finditer(text):
        if not _percent_matches(match.group(1), match.group(2), variance_pct):
            return _abstain("percentage",
                            "Cites a percentage ({0}) that is not the row's own variance %.".format(
                                match.group(0).strip()), target)
    if _OFFSET.search(text):
        return _abstain("offset", "Uses offset or netting language, so the amounts do not simply add.",
                        target)
    if _NEGATIVE.search(text):
        return _abstain("negative", "Cites a negative amount.", target)

    amounts = parse_amounts(text)
    if _compared(text, amounts):
        return _abstain("comparison", "Compares one amount against another rather than sizing drivers.",
                        target, [a.value for a in amounts])

    candidates = [a for a in amounts if not a.per_unit]
    if not candidates:
        return _abstain("no_amounts", "Cites no driver amounts to test.", target)

    direction = 1 if variance_usd > 0 else (-1 if variance_usd < 0 else 0)
    base_tol = max(float(tolerance_usd), float(tolerance_pct) * target)
    cited = tuple(a.value for a in candidates)

    balances = [abs(float(b)) for b in (prior, current) if b is not None]
    readings = [("as cited", candidates)]
    as_balances = [a for a in candidates
                   if not any(_close(a.value, b, a.precision) for b in balances)]
    if len(as_balances) != len(candidates):
        readings.append(("balances set aside", as_balances))

    # Either reading may pass. When a balance-citation reading exists it is the
    # more specific one, so its verdict decides; otherwise the as-cited verdict
    # stands. A verdict is clean (pass), mismatch (fail) or trouble (abstain).
    verdicts = []
    for label, reading in readings:
        verdict = _judge(reading, target, base_tol, direction, label)
        if verdict.result == PASS:
            return verdict._replace(cited=cited)
        verdicts.append(verdict)
    return verdicts[-1]._replace(cited=cited)


def _judge(reading: List[Amount], target: float, base_tol: float, direction: int,
           label: str) -> ArithResult:
    # The restated total carries the row's own direction, so only the other
    # amounts are tested for moving against it.
    total = next((a for a in reading if _close(a.value, target, base_tol + a.precision)), None)
    drivers_only = [a for a in reading if a is not total]
    if direction and any(a.sign == -direction for a in drivers_only):
        return _abstain("direction",
                        "A driver moves against the variance, so the amounts net rather than add.",
                        target)
    status, drivers, tolerance, why = _reconcile(reading, target, base_tol)
    if status == "clean":
        if drivers:
            detail = "Drivers {0} = {1} against a {2} variance (tolerance {3}, {4}).".format(
                " + ".join(_money(d) for d in drivers), _money(sum(drivers)), _money(target),
                _money(tolerance), label)
        else:
            detail = "Cites the variance amount itself ({0}), {1}.".format(_money(target), label)
        return ArithResult(PASS, "reconciled", detail, (), tuple(drivers), target, tolerance)
    if status == "mismatch":
        detail = "Drivers {0} = {1} against a {2} variance; off by {3}, tolerance {4}.".format(
            " + ".join(_money(d) for d in drivers), _money(sum(drivers)), _money(target),
            _money(abs(sum(drivers) - target)), _money(tolerance))
        return ArithResult(FAIL, "mismatch", detail, (), tuple(drivers), target, tolerance)
    if status == "empty":
        return _abstain("no_amounts", "Cites no driver amounts besides balances.", target)
    return _abstain(status, why[0].upper() + why[1:] + ".", target)
