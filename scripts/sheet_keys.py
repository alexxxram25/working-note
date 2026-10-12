#!/usr/bin/env python3
"""Issue, list and revoke sheet keys: the fallback when identity tokens are not used.

    python scripts/sheet_keys.py issue --sheet <spreadsheet id> --role preparer
    python scripts/sheet_keys.py issue --sheet <spreadsheet id> --role reviewer --save-as COACH_SHEET_KEY
    python scripts/sheet_keys.py list
    python scripts/sheet_keys.py revoke k_1a2b3c4d

A key is scoped to one spreadsheet, capped at a role, rate-limited and
revocable. Only its SHA-256 is stored; the key itself is printed once, at
issue, and cannot be recovered. A key cannot say who is using it, so
overrides made with one are stored as unverified.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from coach import envfile  # noqa: E402
from coach.auth import issue_key  # noqa: E402
from coach.store.postgres import PostgresStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("--sheet", required=True, help="the spreadsheet ID the key may be used on")
    issue.add_argument("--role", choices=["preparer", "reviewer"], required=True)
    issue.add_argument("--label", default=None)
    issue.add_argument("--rate", type=int, default=30, help="requests per minute")
    issue.add_argument("--save-as", metavar="VAR", default=None,
                       help="write the key to backend/.env as VAR instead of printing it")
    sub.add_parser("list")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("key_id")
    args = parser.parse_args()

    envfile.load()
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("DATABASE_URL is not set (add it to backend/.env with: open -e backend/.env)")
    db = PostgresStore(dsn)

    if args.command == "issue":
        env_path = envfile.default_path()
        if args.save_as and (env_path is None or not re.match(r"^[A-Z][A-Z0-9_]*$", args.save_as)):
            raise SystemExit("--save-as needs an UPPER_CASE name and an env file (COACH_ENV_FILE is 'none')")
        try:
            raw, record = issue_key(args.sheet, args.role, args.label, args.rate)
        except ValueError as exc:
            raise SystemExit(str(exc))
        db.add_sheet_key(record, created_by="scripts/sheet_keys.py")
        print("Key {0} for sheet {1} ({2}, {3}/min).".format(record.key_id, args.sheet, args.role, args.rate))
        if args.save_as:
            envfile.set_value(env_path, args.save_as, raw, comment="Sheet key {0} ({1}, {2}); revoke with "
                              "scripts/sheet_keys.py revoke {0}".format(record.key_id, args.role, args.label or ""))
            print("Saved to {0} as {1} (not shown).".format(env_path.name, args.save_as))
        else:
            print("Shown once; store it now (Coach > Admin > Configure service, or a password manager):")
            print(raw)
        print("RAN: key issued")
    elif args.command == "list":
        with db._connect() as conn:
            rows = conn.execute("select key_id, sheet_id, role_ceiling, label, rate_limit_per_minute,"
                                " created_at, last_used_at, revoked_at from sheet_keys order by created_at"
                                ).fetchall()
        for row in rows:
            print("{0:<12} {1:<46} {2:<9} {3:<8} {4}".format(
                row["key_id"], row["sheet_id"], row["role_ceiling"],
                "revoked" if row["revoked_at"] else "active", row["label"] or ""))
        print("RAN: {0} key(s)".format(len(rows)))
    else:
        if not db.revoke_sheet_key(args.key_id):
            print("no active key {0}".format(args.key_id), file=sys.stderr)
            return 1
        print("RAN: {0} revoked".format(args.key_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
