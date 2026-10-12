#!/usr/bin/env python3
"""Manage who may use the Coach, and as what.

    python scripts/team.py list
    python scripts/team.py add someone@yourco.com --role reviewer --name "Someone"
    python scripts/team.py deactivate someone@yourco.com

Reads DATABASE_URL from the environment or backend/.env and never prints it.
Roles are enforced by the service on every request, so a change here applies
on the person's next action; their menu catches up with Coach > Admin >
Refresh my role.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from coach import envfile  # noqa: E402
from coach.store.postgres import PostgresStore  # noqa: E402


def store() -> PostgresStore:
    envfile.load()
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("DATABASE_URL is not set (add it to backend/.env with: open -e backend/.env)")
    return PostgresStore(dsn)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("email")
    add.add_argument("--role", choices=["preparer", "reviewer"], required=True)
    add.add_argument("--name", default=None)
    off = sub.add_parser("deactivate")
    off.add_argument("email")
    args = parser.parse_args()

    db = store()
    if args.command == "list":
        for member in db.team_members():
            print("{0:<40} {1:<9} {2:<8} {3}".format(member.email, member.role,
                                                    "active" if member.active else "inactive",
                                                    member.display_name or ""))
        print("RAN: {0} team member(s)".format(len(db.team_members())))
    elif args.command == "add":
        db.upsert_team_member(args.email.strip().lower(), args.role, args.name)
        print("RAN: {0} is a {1}".format(args.email.strip().lower(), args.role))
    else:
        member = db.team_member(args.email)
        if member is None:
            print("{0} is not on the team list".format(args.email), file=sys.stderr)
            return 1
        db.upsert_team_member(member.email, member.role, member.display_name, active=False)
        print("RAN: {0} deactivated".format(member.email))
    return 0


if __name__ == "__main__":
    sys.exit(main())
