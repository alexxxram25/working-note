"""Line IDs: the persistent composite key every result attaches to.

A Line ID is built once from entity, close period, comparison basis, account
and any dimensional slice, and never changes afterwards. Write-back, attempt
counting and the concurrency checks all key on it rather than on a row number
or the account text, because rows get sorted and account names get edited;
the account is display only once the ID exists.

    FHD-US|2026-09|MoM|5000
    FHD-US|2026-09|MoM|6000|PLAT

The Apps Script assigns IDs to blank rows with the same rule (shared test
vectors keep the two implementations identical) and never overwrites one.
A new close period produces a new Line ID, which is what makes "a new close is
a new line" true for attempt counting.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

SEPARATOR = "|"

_LEADING_NUMBER = re.compile(r"^\s*(\d[\d.\-]*)")
_WHITESPACE = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^A-Za-z0-9]+")


class LineIdError(ValueError):
    """A Line ID or one of its parts is missing or malformed."""


class LineIdParts(NamedTuple):
    entity: str
    period: str
    basis: str
    account_key: str
    slice: Optional[str] = None


def _clean(value: object, name: str) -> str:
    text = _WHITESPACE.sub(" ", str(value if value is not None else "")).strip()
    if not text:
        raise LineIdError("{0} is empty".format(name))
    if SEPARATOR in text:
        raise LineIdError("{0} may not contain {1!r}".format(name, SEPARATOR))
    return text


def account_key(account: str) -> str:
    """The account's number when it leads with one, else a slug of its name.

    "6000 Engineering salaries" -> "6000"; "Cloud hosting" -> "CLOUD-HOSTING".
    """
    text = _clean(account, "account")
    match = _LEADING_NUMBER.match(text)
    if match:
        return match.group(1).rstrip(".-")
    slug = _NON_SLUG.sub("-", text).strip("-").upper()
    if not slug:
        raise LineIdError("account has no usable characters")
    return slug


def build_line_id(
    entity: str,
    period: str,
    basis: str,
    account: str,
    slice: Optional[str] = None,
) -> str:
    parts = [
        _clean(entity, "entity"),
        _clean(period, "period"),
        _clean(basis, "comparison basis"),
        account_key(account),
    ]
    if slice is not None and str(slice).strip():
        parts.append(_clean(slice, "slice"))
    return SEPARATOR.join(parts)


def parse_line_id(line_id: str) -> LineIdParts:
    """Split and validate a Line ID; raises LineIdError when malformed."""
    if line_id is None or not str(line_id).strip():
        raise LineIdError("Line ID is empty")
    text = str(line_id)
    if text != text.strip():
        raise LineIdError("Line ID has leading or trailing whitespace")
    parts = text.split(SEPARATOR)
    if len(parts) not in (4, 5):
        raise LineIdError(
            "Line ID needs entity|period|basis|account[|slice], got {0} part(s)".format(len(parts))
        )
    if any(not part.strip() or part != part.strip() for part in parts):
        raise LineIdError("Line ID has an empty or padded part")
    return LineIdParts(*parts) if len(parts) == 5 else LineIdParts(*parts, None)


def period_of(line_id: str) -> str:
    return parse_line_id(line_id).period
