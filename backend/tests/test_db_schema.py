"""Schema-level guarantees, tested against a real Postgres.

Each test gets its own freshly migrated database so a failure in one cannot
leak state into another. Needs COACH_TEST_DSN pointing at a server where the
login may create databases (the local test cluster: `make db-test`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import errors  # noqa: E402

from conftest import ROOT  # noqa: E402

pytestmark = pytest.mark.postgres

SHIM = ROOT / "db" / "local_supabase_shim.sql"


def _with_db(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


def _migrate(dsn: str, seed: bool = True) -> subprocess.CompletedProcess:
    args = [sys.executable, str(ROOT / "scripts" / "apply_migrations.py")] + (["--seed"] if seed else [])
    return subprocess.run(args, env={**os.environ, "DATABASE_URL": dsn}, capture_output=True, text=True)


@pytest.fixture
def fresh_db(pg_dsn):
    name = "coach_t_" + uuid.uuid4().hex[:10]
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute("create database {0}".format(name))
    dsn = _with_db(pg_dsn, name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SHIM.read_text())
        result = _migrate(dsn)
        assert result.returncode == 0, result.stderr
        assert "RAN:" in result.stdout
        yield dsn
    finally:
        with psycopg.connect(pg_dsn, autocommit=True) as admin:
            admin.execute("drop database if exists {0} with (force)".format(name))


def _run_and_log_row(conn):
    run_id = str(uuid.uuid4())
    conn.execute(
        "insert into runs (run_id, sheet_id, role, auth_method, attribution_verified, status,"
        " settings, question_builder_version, model_pin, jev_mode)"
        " values (%s, 's1', 'preparer', 'local', false, 'running', '{}', 'qb', 'pin', 'replay')",
        (run_id,),
    )
    conn.execute(
        "insert into log (sheet_id, close_period, line_id, run_id, attempt_number, commentary_hash,"
        " criterion_id, result) values ('s1', '2026-09', 'E|2026-09|MoM|1', %s, 1, 'sha256:x', 'C1', 'fail')",
        (run_id,),
    )
    return run_id


@pytest.mark.parametrize("statement", [
    "update log set result = 'pass'",
    "delete from log",
    "truncate log",
])
def test_log_refuses_mutation_even_for_its_owner(fresh_db, statement):
    with psycopg.connect(fresh_db) as conn:
        _run_and_log_row(conn)
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)


def test_log_accepts_inserts_as_coach_service(fresh_db):
    with psycopg.connect(fresh_db) as conn:
        conn.execute("set role coach_service")
        run_id = _run_and_log_row(conn)
        count = conn.execute("select count(*) from log where run_id = %s", (run_id,)).fetchone()[0]
        assert count == 1


@pytest.mark.parametrize("statement", [
    "update log set result = 'pass'",
    "alter table log disable trigger log_append_only_rows",
    "drop trigger log_append_only_rows on log",
])
def test_coach_service_cannot_rewrite_or_unguard_the_log(fresh_db, statement):
    with psycopg.connect(fresh_db) as conn:
        _run_and_log_row(conn)
        conn.execute("set role coach_service")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)


def test_every_append_only_table_refuses_update(fresh_db):
    with psycopg.connect(fresh_db) as conn:
        conn.execute(
            "insert into jev_responses (fingerprint, model_snapshot, question_id, probability, source)"
            " values ('fp', 'jev-1.13.0', 'C1', 0.5, 'recording')"
        )
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("update jev_responses set probability = 0.9")


def test_rounded_cache_rows_from_before_0005_are_never_read(fresh_db):
    """A generation-1 row (rounded) does not answer, and does not block the exact answer being cached."""
    from coach.jev.client import JevAnswer
    from coach.store.postgres import PostgresStore

    with psycopg.connect(fresh_db) as conn:
        conn.execute(
            "insert into jev_responses (fingerprint, precision_generation, model_snapshot, question_id,"
            " probability, source) values ('q:old', 1, 'typesafe/jev-1.13-20260917', 'C1', 0.9000, 'live')")
        column = conn.execute("select data_type from information_schema.columns"
                              " where table_name = 'jev_responses' and column_name = 'probability'").fetchone()[0]
    assert column == "double precision"
    store = PostgresStore(fresh_db)
    assert store.jev_cache_get("q:old") is None
    store.jev_cache_put("q:old", "C1", JevAnswer(0.89996, "typesafe/jev-1.13-20260917", "live"), {})
    assert store.jev_cache_get("q:old").probability == 0.89996


def test_cache_rows_an_older_service_writes_after_the_migrations_are_never_read(fresh_db):
    """Migrate first, deploy later: whatever the still-running old service caches in between is legacy.

    The old service omits precision_generation and inserts the probability rounded
    to four decimals. 0005 made 2 the column default, so such a row looked exact
    and a cached rerun passed a line the live run had failed. 0007 puts the default
    back to 1, and the service reads a generation that was never a default.
    """
    from coach.jev.client import JevAnswer
    from coach.jev.thresholds import grade
    from coach.store.postgres import JEV_CACHE_GENERATION, PostgresStore

    with psycopg.connect(fresh_db) as conn:
        # Exactly the old service's statement: no generation named, value already rounded.
        conn.execute(
            "insert into jev_responses (fingerprint, model_snapshot, question_id, probability, source,"
            " input_tokens, cost_usd) values ('q:legacy', 'typesafe/jev-1.13-20260917', 'C1', %s, 'live', 10, null)",
            (round(0.89996, 4),))
        # And a row from the window in which 2 was the default.
        conn.execute(
            "insert into jev_responses (fingerprint, precision_generation, model_snapshot, question_id,"
            " probability, source) values ('q:window', 2, 'typesafe/jev-1.13-20260917', 'C1', 0.9, 'live')")
        default = conn.execute("select column_default from information_schema.columns where table_name ="
                               " 'jev_responses' and column_name = 'precision_generation'").fetchone()[0]
        legacy_generation = conn.execute(
            "select precision_generation from jev_responses where fingerprint = 'q:legacy'").fetchone()[0]
    assert default == "1" and legacy_generation == 1
    assert JEV_CACHE_GENERATION not in (1, 2), "the service must read a generation that was never a default"
    store = PostgresStore(fresh_db)
    assert store.jev_cache_get("q:legacy") is None and store.jev_cache_get("q:window") is None
    # The live answer is cached exactly and grades as the live run did; the legacy row never speaks.
    store.jev_cache_put("q:legacy", "C1", JevAnswer(0.89996, "typesafe/jev-1.13-20260917", "live"), {})
    cached = store.jev_cache_get("q:legacy").probability
    assert cached == 0.89996 and grade(cached, 0.9, True).result == "fail"


def _store_run(store, sheet, run_id):
    store.create_run(dict(run_id=run_id, sheet_id=sheet, role="preparer", auth_method="local",
                          attribution_verified=False, settings={}, question_builder_version="qb",
                          model_pin="jev-1.13.0", jev_mode="replay"))


def _hold_lease_row(dsn, sheet):
    """A transaction holding the lease row's lock, the way a committing fence does."""
    conn = psycopg.connect(dsn)
    conn.execute("select 1 from run_locks where sheet_id = %s for update", (sheet,))
    return conn


def test_the_fence_judges_expiry_after_waiting_for_the_lease_row(fresh_db):
    """now() is fixed at transaction start; a fence that waited must use the clock after the wait."""
    import threading
    import time

    from coach.models import LineIn
    from coach.store.base import LeaseLost
    from coach.store.postgres import PostgresStore

    store = PostgresStore(fresh_db)
    run_id = str(uuid.uuid4())
    assert store.acquire_lock("s1", run_id, "a@example.com", 1).acquired  # a one-second lease
    _store_run(store, "s1", run_id)
    blocker = _hold_lease_row(fresh_db, "s1")
    outcome = {}

    def write():
        try:
            store.record_evaluation("s1", run_id, [LineIn(line_id="E|2026-09|MoM|1", commentary="x")], [dict(
                close_period="2026-09", line_id="E|2026-09|MoM|1", attempt_number=1, commentary_hash="h",
                criterion_id="C1", result="pass")], {"lines_evaluated": 1})
            outcome["wrote"] = True
        except LeaseLost:
            outcome["lost"] = True

    writer = threading.Thread(target=write)
    writer.start()
    time.sleep(1.5)  # the writer is queued behind the blocker; the lease expires meanwhile
    blocker.rollback()
    blocker.close()
    writer.join(timeout=10)
    assert outcome == {"lost": True}, outcome
    with psycopg.connect(fresh_db) as conn:
        assert conn.execute("select count(*) from log").fetchone()[0] == 0
        assert conn.execute("select count(*) from lines").fetchone()[0] == 0


def test_renewal_after_waiting_does_not_revive_an_expired_lease(fresh_db):
    import threading
    import time

    from coach.store.postgres import PostgresStore

    store = PostgresStore(fresh_db)
    run_id = str(uuid.uuid4())
    assert store.acquire_lock("s1", run_id, "a@example.com", 1).acquired
    blocker = _hold_lease_row(fresh_db, "s1")
    outcome = {}
    renewer = threading.Thread(target=lambda: outcome.setdefault("renewed", store.renew_lock("s1", run_id, 600)))
    renewer.start()
    time.sleep(1.5)
    blocker.rollback()
    blocker.close()
    renewer.join(timeout=10)
    assert outcome == {"renewed": False}
    # Expired means expired: the next run takes the sheet.
    assert store.acquire_lock("s1", str(uuid.uuid4()), "b@example.com", 60).acquired


def test_criteria_recorded_by_an_older_service_are_reconciled_on_the_next_run(fresh_db):
    """Migrate first, deploy later: the old service advances the history but never criteria_current.

    Old service records A (the pointer is backfilled to A), then records B: history
    is at version 2 (B), the pointer still says A. The new service then receives A.
    Trusting the pointer alone it would report no change and log version 2, which
    describes B, against text graded as A.
    """
    from fastapi.testclient import TestClient

    import fernhill
    from coach import criteria_versions
    from coach.api import build_services, create_app
    from coach.config import Config
    from coach.models import Criterion
    from helpers import FakeJev

    services = build_services(Config(env="development", store="postgres", database_url=fresh_db,
                                     auth_modes=("local",), jev_mode="replay"))
    services.jev = FakeJev()
    client = TestClient(create_app(services=services), raise_server_exceptions=False)
    sheet = "s1"
    rows_a = fernhill.criteria_rows()
    body = {"sheet_id": sheet, "criteria": rows_a, "settings": fernhill.settings_rows(),
            "lines": [l.model_dump() for l in fernhill.lines_for("2026-09")]}
    first = client.post("/v1/runs", json=body)
    assert first.status_code == 200, first.text
    client.post("/v1/runs/{0}/complete".format(first.json()["run_id"]), json={"sheet_id": sheet})

    # The old service's write of B: a snapshot row and a history row, no pointer update.
    rows_b = [dict(r) for r in rows_a]
    rows_b[0]["Feedback when failed"] = "B: who or what, specifically?"
    digest_b, content_b = criteria_versions.snapshot([Criterion.from_sheet_row(r) for r in rows_b])
    c1_b = Criterion.from_sheet_row(rows_b[0])
    with psycopg.connect(fresh_db) as conn:
        from psycopg.types.json import Jsonb
        snap = conn.execute(
            "insert into criteria_snapshots (sheet_id, content_hash, criteria, captured_by) values (%s, %s, %s, 'old')"
            " returning id", (sheet, digest_b, Jsonb(content_b))).fetchone()[0]
        conn.execute(
            "insert into criteria_history (sheet_id, criterion_id, version, content, content_hash, fields_changed,"
            " diff, calibration_impact, snapshot_id) values (%s, 'C1', 2, %s, %s, %s, '{}', 'none', %s)",
            (sheet, Jsonb(c1_b.sheet_content()), criteria_versions.content_hash(c1_b.sheet_content()),
             ["Feedback when failed"], snap))
        conn.commit()
        pointer = conn.execute("select content_hash from criteria_current where sheet_id = %s", (sheet,)).fetchone()[0]
    assert pointer == criteria_versions.snapshot([Criterion.from_sheet_row(r) for r in rows_a])[0]

    second = client.post("/v1/runs", json=body)  # the tab shows A
    assert second.status_code == 200, second.text
    out = second.json()
    change = {c["criterion_id"]: c for c in out["criteria_changes"]}["C1"]
    assert change["version"] == 3 and change["before"].startswith("Feedback when failed: B:")
    assert {c["id"]: c["version"] for c in out["criteria_status"]}["C1"] == 3
    with psycopg.connect(fresh_db) as conn:
        versions = conn.execute(
            "select distinct criterion_version from log where run_id = %s and criterion_id = 'C1'",
            (out["run_id"],)).fetchall()
        current = conn.execute("select content_hash from criteria_current where sheet_id = %s", (sheet,)).fetchone()[0]
    assert [v[0] for v in versions] == [3], "the Log carries the version that describes the tab as graded"
    assert current == pointer


def test_resubmitted_override_is_a_no_op(fresh_db):
    insert = (
        "insert into overrides (sheet_id, close_period, line_id, kind, criterion_id, not_covered,"
        " note, note_hash, submitted_by, role, attribution_verified)"
        " values ('s1', '2026-09', 'E|2026-09|MoM|1', 'missed_issue', null, true, 'n', 'h', null,"
        " 'preparer', false) on conflict do nothing"
    )
    with psycopg.connect(fresh_db) as conn:
        assert conn.execute(insert).rowcount == 1
        # NULL criterion and NULL submitter must still collide: NULLS NOT DISTINCT.
        assert conn.execute(insert).rowcount == 0


def test_second_attempts_view_counts_only_revisions_after_a_fail(fresh_db):
    with psycopg.connect(fresh_db) as conn:
        run_id = str(uuid.uuid4())
        conn.execute(
            "insert into runs (run_id, sheet_id, role, auth_method, attribution_verified, status,"
            " settings, question_builder_version, model_pin, jev_mode)"
            " values (%s, 's1', 'preparer', 'local', false, 'completed', '{}', 'qb', 'pin', 'replay')",
            (run_id,),
        )
        rows = [
            # line A: fails C1 on attempt 1, passes on attempt 2 -> one C1 second attempt
            ("A", 1, "C1", "fail"), ("A", 1, "C2", "pass"), ("A", 2, "C1", "pass"),
            # line B: fails C3 and never comes back -> no second attempt
            ("B", 1, "C3", "fail"),
            # line C: passes everything -> nothing
            ("C", 1, "C1", "pass"),
        ]
        for line, attempt, cid, result in rows:
            conn.execute(
                "insert into log (sheet_id, close_period, line_id, run_id, attempt_number,"
                " commentary_hash, criterion_id, result) values ('s1', '2026-09', %s, %s, %s, 'h', %s, %s)",
                (line, run_id, attempt, cid, result),
            )
        got = conn.execute(
            "select criterion_id, lines_with_second_attempt from second_attempts"
            " where sheet_id = 's1' order by criterion_id"
        ).fetchall()
        assert got == [("C1", 1)]


def test_browser_roles_hold_nothing(fresh_db):
    with psycopg.connect(fresh_db) as conn:
        for role in ("anon", "authenticated"):
            for table in ("log", "team_roles", "sheet_keys", "second_attempts"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    held = conn.execute(
                        "select has_table_privilege(%s, %s, %s)", (role, table, privilege)
                    ).fetchone()[0]
                    assert not held, (role, table, privilege)


def test_verify_db_passes_on_a_clean_schema(fresh_db):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_db.py")],
        env={**os.environ, "DATABASE_URL": fresh_db}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RAN: verify_db" in result.stdout
    assert ", 0 skipped" in result.stdout


def test_verify_db_catches_a_missing_trigger(fresh_db):
    """The verifier itself must be able to fail: drop a guard and it has to say so."""
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute("drop trigger log_append_only_rows on log")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_db.py")],
        env={**os.environ, "DATABASE_URL": fresh_db}, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "FAIL  trigger log_append_only_rows present and enabled" in result.stdout
    assert "FAIL  UPDATE on log was ALLOWED" in result.stdout


def test_migration_runner_refuses_an_edited_migration(fresh_db):
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute("update schema_migrations set checksum = 'tampered' where version = '0001_core'")
    result = _migrate(fresh_db, seed=False)
    assert result.returncode == 1
    assert "REFUSED: 0001_core.sql" in result.stderr


def test_seed_is_idempotent(fresh_db):
    again = _migrate(fresh_db)
    assert again.returncode == 0, again.stderr
    with psycopg.connect(fresh_db) as conn:
        counts = conn.execute(
            "select (select count(*) from team_roles), (select count(*) from settings),"
            " (select count(*) from criteria_snapshots where sheet_id = '*'), (select count(*) from calibrations),"
            " (select count(*) from jev_responses)"
        ).fetchone()
    assert counts == (0, 17, 1, 0, 0), "the seed adds settings and the starter Criteria, nobody and nothing calibrated"


def _service_login(fresh_db, pg_dsn, extra=""):
    login = "svc_" + uuid.uuid4().hex[:8]
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute("create role {0} login in role coach_service {1}".format(login, extra))
    parts = urlsplit(fresh_db)
    host = parts.netloc.rsplit("@", 1)[-1]
    return login, urlunsplit((parts.scheme, "{0}@{1}".format(login, host), parts.path, parts.query, parts.fragment))


def _drop_login(pg_dsn, login):
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute("drop role if exists {0}".format(login))


def test_verify_db_probes_the_log_as_the_service_login(fresh_db, pg_dsn):
    """On Supabase the owner can't assume coach_service; the service's own login runs the probes instead."""
    login, service_dsn = _service_login(fresh_db, pg_dsn)
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify_db.py")],
            env={**os.environ, "DATABASE_URL": fresh_db, "COACH_SERVICE_DATABASE_URL": service_dsn},
            capture_output=True, text=True,
        )
    finally:
        _drop_login(pg_dsn, login)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RAN: Log probes as the service login ({0})".format(login) in result.stdout
    assert ", 0 skipped" in result.stdout


def test_verify_db_fails_a_service_login_that_could_rewrite_the_log(fresh_db, pg_dsn):
    login, service_dsn = _service_login(fresh_db, pg_dsn, extra="superuser")
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify_db.py")],
            env={**os.environ, "DATABASE_URL": fresh_db, "COACH_SERVICE_DATABASE_URL": service_dsn},
            capture_output=True, text=True,
        )
    finally:
        _drop_login(pg_dsn, login)
    assert result.returncode == 1
    assert "FAIL  the service login is not a superuser" in result.stdout
    assert "FAIL  the service login: disable the Log's triggers was ALLOWED" in result.stdout


def test_create_service_login_stores_only_a_verifier_and_passes_verify_db(fresh_db, pg_dsn, tmp_path):
    login = "svc_" + uuid.uuid4().hex[:8]
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=" + fresh_db + "\n")
    script = str(ROOT / "scripts" / "create_service_login.py")
    clean_env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "COACH_SERVICE_DATABASE_URL")}
    try:
        created = subprocess.run([sys.executable, script, "--login", login, "--env", str(env_file)],
                                 env=clean_env, capture_output=True, text=True)
        assert created.returncode == 0, created.stdout + created.stderr
        assert "RAN: created {0} (member of coach_service)".format(login) in created.stdout
        written = env_file.read_text().split("COACH_SERVICE_DATABASE_URL=", 1)[1].strip()
        password = urlsplit(written).password
        assert password and len(password) >= 40 and password not in created.stdout + created.stderr
        with psycopg.connect(pg_dsn, autocommit=True) as admin:
            stored = admin.execute("select rolpassword from pg_authid where rolname = %s", (login,)).fetchone()[0]
        assert stored.startswith("SCRAM-SHA-256$") and password not in stored

        again = subprocess.run([sys.executable, script, "--login", login, "--env", str(env_file)],
                               env=clean_env, capture_output=True, text=True)
        assert again.returncode == 1 and "already exists" in again.stderr
        rotated = subprocess.run([sys.executable, script, "--login", login, "--env", str(env_file), "--rotate"],
                                 env=clean_env, capture_output=True, text=True)
        assert rotated.returncode == 0 and "RAN: rotated" in rotated.stdout
        assert urlsplit(env_file.read_text().split("COACH_SERVICE_DATABASE_URL=", 1)[1].strip()).password != password
        assert env_file.read_text().count("COACH_SERVICE_DATABASE_URL=") == 1

        verified = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify_db.py")],
            env={**clean_env, "DATABASE_URL": fresh_db,
                 "COACH_SERVICE_DATABASE_URL": env_file.read_text().split("COACH_SERVICE_DATABASE_URL=", 1)[1].strip()},
            capture_output=True, text=True)
        assert verified.returncode == 0, verified.stdout
        assert "RAN: Log probes as the service login ({0})".format(login) in verified.stdout
    finally:
        _drop_login(pg_dsn, login)
