#!/usr/bin/env python3
"""Apply db/migrations/*.sql in order, once each, and optionally db/seed.sql.

    python scripts/apply_migrations.py [--seed]

Reads DATABASE_URL from the environment or backend/.env (the environment wins).

Each migration runs in its own transaction and is recorded in
schema_migrations with a checksum. A migration that was already applied and
has since been edited is refused rather than silently skipped: the database
would no longer match the file a reader is looking at.

Works against Supabase's transaction pooler (prepare_threshold=None, for the
reason given in coach/store/postgres.py) and against the local test cluster.
Never prints the connection string.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from coach import envfile  # noqa: E402
from coach.dsn import DsnError, connect  # noqa: E402

MIGRATIONS = ROOT / "db" / "migrations"
SEED = ROOT / "db" / "seed.sql"


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", action="store_true", help="also apply db/seed.sql (idempotent)")
    args = parser.parse_args()

    envfile.load()
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    files = sorted(MIGRATIONS.glob("*.sql"))
    applied_now = []
    with connect(dsn, prepare_threshold=None, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "create table if not exists schema_migrations ("
                " version text primary key,"
                " checksum text not null,"
                " applied_at timestamptz not null default now())"
            )
            # Bookkeeping only; nobody outside the migration runner needs it.
            cur.execute("alter table schema_migrations enable row level security")
            cur.execute("select version, checksum from schema_migrations")
            done = dict(cur.fetchall())
        conn.commit()

        for path in files:
            version = path.stem
            digest = checksum(path)
            if version in done:
                if done[version] != digest:
                    print("REFUSED: {0} was applied and has since changed on disk".format(path.name),
                          file=sys.stderr)
                    return 1
                continue
            with conn.cursor() as cur:
                cur.execute(path.read_text())
                cur.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (version, digest),
                )
            conn.commit()
            applied_now.append(path.name)

        if args.seed:
            if not SEED.exists():
                print("db/seed.sql is missing; run scripts/write_seed.py", file=sys.stderr)
                return 1
            with conn.cursor() as cur:
                cur.execute(SEED.read_text())
            conn.commit()

    print("RAN: {0} migration(s) applied now, {1} total on file{2}".format(
        len(applied_now), len(files), ", seed applied" if args.seed else ""))
    for name in applied_now:
        print("  applied {0}".format(name))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DsnError as exc:  # already worded without the connection string
        print(exc, file=sys.stderr)
        sys.exit(2)
