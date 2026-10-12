"""Opening database connections without ever repeating the connection string.

A connection string carries the database password, and libpq's parse errors
quote the part of a malformed URI they choke on. For a password with an
unencoded % that part is the password itself, which would then sit in a
terminal, a CI log or the service's deploy log. Everything that connects goes
through connect() here: the string is checked first, problems are described
without quoting it, and any error that still mentions the password has it
removed.
"""

from __future__ import annotations

import re
from typing import Any, List
from urllib.parse import unquote

REDACTED = "(the connection string is not shown: it contains the database password)"
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


class DsnError(ValueError):
    pass


def _userinfo(dsn: str) -> str:
    """Everything between the scheme and the last @: the user and the password."""
    return dsn.split("://", 1)[1].rpartition("@")[0] if "://" in dsn else ""


def problems(dsn: str) -> List[str]:
    """What is wrong with a URI connection string, in words that never quote it."""
    if not dsn or not dsn.strip():
        return ["DATABASE_URL is empty"]
    if dsn != dsn.strip() or dsn[0] in "\"'" or dsn[-1] in "\"'":
        return ["DATABASE_URL has spaces or quotes around it; paste the URI alone"]
    if not dsn.startswith(("postgresql://", "postgres://")):
        return []  # a key=value conninfo string: libpq parses it, and connect() redacts its errors
    if any(c in dsn for c in " \t\n\"'"):
        return ["DATABASE_URL has spaces or quotes in it; paste the URI alone"]
    found = []
    if dsn.split("://", 1)[1].count("@") > 1:
        found.append("the password contains an @, which must be written %40")
    userinfo = _userinfo(dsn)
    if "[" in userinfo or "]" in userinfo:
        found.append("the password has [ ] in it: either the brackets from the [YOUR-PASSWORD] "
                     "placeholder are still there, or they need writing as %5B and %5D")
    if _BAD_PERCENT.search(userinfo):
        found.append("the password contains a % that isn't percent-encoded (write it as %25)")
    if any(c in userinfo for c in "/?#"):
        found.append("the password contains / ? or #, which must be percent-encoded")
    return found


def _secrets(dsn: str) -> List[str]:
    userinfo = _userinfo(dsn)
    password = userinfo.split(":", 1)[1] if ":" in userinfo else ""
    out = [s for s in {password, unquote(password)} if len(s) >= 4]
    # A parse error may quote only a fragment, so also guard every 6-character run of the password.
    for secret in list(out):
        out.extend(secret[i:i + 6] for i in range(0, max(len(secret) - 5, 0)))
    return out


def redact(message: str, dsn: str) -> str:
    if dsn and dsn in message:
        return REDACTED
    for secret in _secrets(dsn):
        if secret and secret in message:
            return REDACTED
    return message


def connect(dsn: str, **kwargs: Any):
    """psycopg.connect, with the string checked first and every error scrubbed of it."""
    import psycopg

    found = problems(dsn)
    if found:
        raise DsnError("DATABASE_URL can't be used: {0}. {1}".format("; ".join(found), REDACTED))
    try:
        return psycopg.connect(dsn, **kwargs)
    except psycopg.ProgrammingError:
        raise DsnError("DATABASE_URL is not a valid connection string. " + REDACTED) from None
    except psycopg.Error as exc:
        message = redact(str(exc), dsn)
        if message == REDACTED:
            raise DsnError("could not connect to the database. " + REDACTED) from None
        raise
