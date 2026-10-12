#!/usr/bin/env python3
"""Check backend/.env's DATABASE_URL without ever printing it.

    python scripts/check_dsn.py                      # what's wrong with it, if anything
    python scripts/check_dsn.py --connect            # and try it: prints the user and Postgres version
    python scripts/check_dsn.py --encode-password    # percent-encode a pasted raw password, in place
    python scripts/check_dsn.py --var COACH_SERVICE_DATABASE_URL --connect   # the service's own login

Passwords with characters such as % @ / ? # [ ] must be percent-encoded in a
connection URI. --encode-password does that to the password currently in the
file, so paste the password raw and run it once; running it again would
encode the % signs it just wrote. It also drops the [ ] left over from
Supabase's [YOUR-PASSWORD] placeholder.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from coach import envfile  # noqa: E402
from coach.dsn import DsnError, connect, problems  # noqa: E402

ENV = envfile.default_path() or Path("/nonexistent/.env")  # COACH_ENV_FILE=none in tests


def encode_password(env_path: Path, var: str = "DATABASE_URL") -> bool:
    """Rewrite the variable's line with its password percent-encoded. True if it changed."""
    pattern = re.compile(r"^(\s*(?:export\s+)?" + re.escape(var) + r"\s*=\s*)(.*?)(\s*)$")
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        match = pattern.match(line.rstrip("\n"))
        if not match or "://" not in match.group(2):
            continue
        value = match.group(2).strip("\"'")
        scheme, rest = value.split("://", 1)
        userinfo, at, host = rest.rpartition("@")
        if not at or ":" not in userinfo:
            continue
        user, password = userinfo.split(":", 1)
        if password.startswith("[") and password.endswith("]"):
            password = password[1:-1]
        new = "{0}://{1}:{2}@{3}".format(scheme, user, quote(password, safe=""), host)
        if new != value:
            lines[i] = match.group(1) + new + "\n"
            changed = True
    if changed:
        env_path.write_text("".join(lines), encoding="utf-8")
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, default=ENV)
    parser.add_argument("--var", default="DATABASE_URL", help="which variable in the file to check")
    parser.add_argument("--encode-password", action="store_true")
    parser.add_argument("--connect", action="store_true")
    args = parser.parse_args(argv)

    if args.encode_password:
        print("password percent-encoded in place" if encode_password(args.env, args.var)
              else "nothing to encode")
    dsn = envfile.parse(args.env).get(args.var, "")
    found = problems(dsn)
    for problem in found:
        print("PROBLEM " + problem)
    if found:
        return 1
    print("RAN: {0} is well formed (not shown)".format(args.var))
    if args.connect:
        try:
            with connect(dsn, prepare_threshold=None, connect_timeout=15) as conn:
                user, version = conn.execute("select current_user, current_setting('server_version')").fetchone()
        except DsnError as exc:
            print(exc)
            return 1
        print("RAN: connected as {0}, Postgres {1}".format(user, version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
