#!/usr/bin/env python3
"""Write db/seed.sql from the starter Criteria and the Settings defaults.

    python scripts/write_seed.py           # rewrite db/seed.sql
    python scripts/write_seed.py --check   # exit 1 if db/seed.sql is out of date

Run it after editing backend/coach/starter_criteria.csv or a default in
backend/coach/sheet_schema.py. The test suite runs the check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from coach.seed import seed_sql  # noqa: E402

SEED = ROOT / "db" / "seed.sql"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare instead of writing")
    args = parser.parse_args()
    expected = seed_sql()
    if args.check:
        current = SEED.read_text(encoding="utf-8") if SEED.exists() else ""
        if current != expected:
            print("db/seed.sql is out of date; run scripts/write_seed.py", file=sys.stderr)
            return 1
        print("RAN: db/seed.sql matches the starter Criteria and Settings")
        return 0
    SEED.write_text(expected, encoding="utf-8")
    print("RAN: wrote db/seed.sql")
    return 0


if __name__ == "__main__":
    sys.exit(main())
