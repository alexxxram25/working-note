"""The materiality gate: the first check, and the one that keeps Jev's bill honest.

A line is skipped entirely when its variance is under BOTH thresholds, the $
one and the % one. Over either and it is material. A variance exactly equal to
a threshold is not "under" it, so it is material.

When Prior is zero the % variance is undefined. Treating it as infinite would
make every new $500 expense line material and send it to Jev; treating it as
zero lets the $ test decide alone, which is what a reviewer would do.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

# Variance inputs arrive from a spreadsheet as floats. Rounding before the
# comparison keeps 0.1 computed as 0.09999999999999999 from flipping a line
# that sits exactly on the % threshold.
_PCT_PLACES = 9
_USD_PLACES = 2


class Variance(NamedTuple):
    usd: float
    pct: Optional[float]  # a fraction (0.0447 is 4.47%), None when prior is zero


class Materiality(NamedTuple):
    material: bool
    reason: str


def variance_of(
    prior: Optional[float],
    current: Optional[float],
    variance_usd: Optional[float] = None,
    variance_pct: Optional[float] = None,
) -> Variance:
    """Use the sheet's variance cells when present, else derive them.

    The sheet is authoritative for the figures a preparer sees, so a supplied
    Variance ($) is used as-is; it is only derived when blank.
    """
    if variance_usd is None:
        if prior is None or current is None:
            raise ValueError("need Variance ($) or both Prior and Current")
        variance_usd = float(current) - float(prior)
    if variance_pct is None and prior not in (None, 0, 0.0):
        variance_pct = float(variance_usd) / abs(float(prior))
    return Variance(float(variance_usd), None if variance_pct is None else float(variance_pct))


def assess(variance: Variance, threshold_usd: float, threshold_pct: float) -> Materiality:
    usd = round(abs(variance.usd), _USD_PLACES)
    under_usd = usd < round(float(threshold_usd), _USD_PLACES)
    if variance.pct is None:
        under_pct = True
        pct_text = "% undefined (prior is zero)"
    else:
        pct = round(abs(variance.pct), _PCT_PLACES)
        under_pct = pct < round(float(threshold_pct), _PCT_PLACES)
        pct_text = "{0:.1%} vs {1:.1%}".format(abs(variance.pct), float(threshold_pct))
    material = not (under_usd and under_pct)
    reason = "${0:,.0f} vs ${1:,.0f}; {2}".format(usd, float(threshold_usd), pct_text)
    return Materiality(material, reason)
