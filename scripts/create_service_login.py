#!/usr/bin/env python3
"""Create the login the deployed service uses: a member of coach_service and nothing more.

    python scripts/create_service_login.py            # create coach_login, write its connection string
    python scripts/create_service_login.py --rotate   # give coach_login a new password

Connects with the owner's DATABASE_URL from backend/.env, generates a random
password, and sends Postgres only its SCRAM-SHA-256 verifier, computed here, so
the password itself is never in a SQL statement, a server log or an editor's
history. The new login's connection string goes into backend/.env as
COACH_SERVICE_DATABASE_URL: the same host and database as DATABASE_URL, with
Supabase's pooler user name (coach_login.<project-ref>). Nothing secret is
printed. Railway's DATABASE_URL takes that value; the owner's string stays in
backend/.env for the admin scripts.

The login can add to the Log and the other history tables but can't change,
delete or unguard them; scripts/verify_db.py proves that against it.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from coach import envfile  # noqa: E402
from coach.dsn import DsnError, connect  # noqa: E402

ENV = envfile.default_path() or Path("/nonexistent/.env")  # COACH_ENV_FILE=none in tests
VAR = "COACH_SERVICE_DATABASE_URL"


def service_dsn(owner_dsn: str, login: str, password: str) -> str:
    """The owner's URI with the login swapped in, keeping Supabase's .<project-ref> user suffix."""
    scheme, rest = owner_dsn.split("://", 1)
    userinfo, _, host = rest.rpartition("@")
    owner = userinfo.split(":", 1)[0]
    suffix = owner[owner.index("."):] if "." in owner else ""
    return "{0}://{1}{2}:{3}@{4}".format(scheme, login, suffix, quote(password, safe=""), host)


def write_env(env_path: Path, value: str) -> None:
    envfile.set_value(env_path, VAR, value, comment=(
        "The deployed service's own login (scripts/create_service_login.py). Railway's\n"
        "DATABASE_URL takes this value; DATABASE_URL above stays the owner's, for admin scripts."))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--login", default="coach_login")
    parser.add_argument("--rotate", action="store_true", help="new password for an existing login")
    parser.add_argument("--env", type=Path, default=ENV)
    args = parser.parse_args(argv)
    if not re.match(r"^[a-z_][a-z0-9_]{0,40}$", args.login):
        print("--login takes lowercase letters, digits and _", file=sys.stderr)
        return 2

    owner_dsn = envfile.parse(args.env).get("DATABASE_URL", "")
    if not owner_dsn.startswith(("postgresql://", "postgres://")):
        print("DATABASE_URL in {0} must be the owner's postgresql:// URI".format(args.env), file=sys.stderr)
        return 2
    password = secrets.token_urlsafe(32)  # letters, digits, - and _: nothing to encode

    from psycopg import sql

    try:
        with connect(owner_dsn, prepare_threshold=None, autocommit=True) as conn:
            exists = conn.execute("select 1 from pg_roles where rolname = %s", (args.login,)).fetchone()
            if exists and not args.rotate:
                print("{0} already exists; use --rotate to give it a new password".format(args.login),
                      file=sys.stderr)
                return 1
            if not exists and args.rotate:
                print("{0} doesn't exist yet; run without --rotate".format(args.login), file=sys.stderr)
                return 1
            if not conn.execute("select 1 from pg_roles where rolname = 'coach_service'").fetchone():
                print("coach_service is missing: run scripts/apply_migrations.py first", file=sys.stderr)
                return 1
            verifier = conn.pgconn.encrypt_password(password.encode(), args.login.encode(),
                                                    b"scram-sha-256").decode()
            if args.rotate:
                conn.execute(sql.SQL("alter role {0} with login password {1}").format(
                    sql.Identifier(args.login), sql.Literal(verifier)))
            else:
                conn.execute(sql.SQL("create role {0} with login password {1} in role coach_service").format(
                    sql.Identifier(args.login), sql.Literal(verifier)))
    except DsnError as exc:
        print(exc, file=sys.stderr)
        return 2

    value = service_dsn(owner_dsn, args.login, password)
    write_env(args.env, value)
    try:
        with connect(value, prepare_threshold=None, connect_timeout=15) as conn:
            user = conn.execute("select current_user").fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - reported without the string
        print("{0} {1} in {2}, but connecting with it failed ({3}). Poolers can take a minute to see a "
              "new login; try `python scripts/check_dsn.py --var {1} --connect`.".format(
                  "rotated" if args.rotate else "created", VAR, args.env.name, type(exc).__name__))
        return 1
    print("RAN: {0} {1} (member of coach_service); {2} written to {3} (not shown); connected as {4}".format(
        "rotated" if args.rotate else "created", args.login, VAR, args.env.name, user))
    return 0


if __name__ == "__main__":
    sys.exit(main())
