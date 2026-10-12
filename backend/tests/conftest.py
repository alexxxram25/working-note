"""Shared fixtures and the two guards that keep a quiet green honest.

1. `live` tests call real external APIs. They are skipped unless COACH_LIVE=1,
   so the default suite never spends money or depends on the network.
2. `postgres` tests skip when COACH_TEST_DSN is unset, which is fine on a
   laptop without a database. But when COACH_TEST_DSN *is* set, a skipped or
   absent postgres test means the database half of the suite did not run, and
   the session fails instead of reporting green.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
FIXTURES = BACKEND / "tests" / "fixtures" / "fernhill"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# backend/.env holds real credentials (the Supabase owner, the Jev key). No test,
# and no script a test starts, reads it: subprocesses inherit this.
os.environ["COACH_ENV_FILE"] = "none"

_PG = {"passed": 0, "skipped": 0, "failed": 0}


def pytest_collection_modifyitems(config, items):
    if os.environ.get("COACH_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="calls a real API; set COACH_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def pytest_runtest_logreport(report):
    if "postgres" not in report.keywords:
        return
    if report.skipped:
        _PG["skipped"] += 1
    elif report.failed:
        _PG["failed"] += 1
    elif report.when == "call" and report.passed:
        _PG["passed"] += 1


def pytest_sessionfinish(session, exitstatus):
    if not os.environ.get("COACH_TEST_DSN"):
        return
    if _PG["skipped"] or not _PG["passed"]:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        sys.stderr.write(
            "\nFAIL: COACH_TEST_DSN is set but postgres tests passed={passed} "
            "skipped={skipped}. The database half of the suite did not run.\n".format(**_PG)
        )
    else:
        sys.stdout.write("\nRAN: {passed} postgres tests against COACH_TEST_DSN\n".format(**_PG))


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def pg_dsn() -> str:
    dsn = os.environ.get("COACH_TEST_DSN")
    if not dsn:
        pytest.skip("COACH_TEST_DSN not set")
    return dsn
