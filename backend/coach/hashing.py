"""Hashes that decide what counts as "the same".

Commentary hash: SHA-256 of the commentary after Unicode NFC normalization,
whitespace collapsed, ends trimmed. A trailing space or a double space is not a
new attempt; any change to the words is. The hash is what decides whether a
rerun counts as a second attempt, so it is deliberately forgiving of layout and
strict about content.

Content hash: SHA-256 of canonical JSON (sorted keys, no spaces). Used for
criteria snapshots and fingerprints, where two dicts with the same content
must hash the same however they were built.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

PREFIX = "sha256:"
_WHITESPACE = re.compile(r"\s+")


def normalize_commentary(text: Any) -> str:
    if text is None:
        return ""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", str(text))).strip()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def commentary_hash(text: Any) -> str:
    return PREFIX + sha256_hex(normalize_commentary(text))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    return PREFIX + sha256_hex(canonical_json(value))
