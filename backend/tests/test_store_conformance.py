"""One suite, two stores: MemoryStore always, PostgresStore when COACH_TEST_DSN is set.

The two may differ in how they write and must agree on every answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

from coach import criteria_versions
from coach.attempts import PriorAttempt
from coach.hashing import commentary_hash
from coach.jev.client import JevAnswer
from coach.models import Criterion, LineIn
from coach.store.base import SheetKey
from coach.store.memory import MemoryStore
from conftest import ROOT

SHEET = "sheet-1"


def _postgres_store(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from coach.store.postgres import PostgresStore

    name = "coach_s_" + uuid.uuid4().hex[:10]
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute("create database {0}".format(name))
    parts = urlsplit(pg_dsn)
    dsn = urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "local_supabase_shim.sql").read_text())
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "apply_migrations.py")],
                            env={**os.environ, "DATABASE_URL": dsn}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    def drop():
        with psycopg.connect(pg_dsn, autocommit=True) as admin:
            admin.execute("drop database if exists {0} with (force)".format(name))

    return PostgresStore(dsn), drop


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    dsn = os.environ.get("COACH_TEST_DSN")
    if not dsn:
        pytest.skip("COACH_TEST_DSN not set")
    pg, drop = _postgres_store(dsn)
    try:
        yield pg
    finally:
        drop()


def new_run(store, sheet=SHEET, **extra):
    run_id = str(uuid.uuid4())
    store.create_run(dict(run_id=run_id, sheet_id=sheet, role="preparer", auth_method="local",
                          attribution_verified=False, settings={}, question_builder_version="qb-1.0.0",
                          model_pin="jev-1.13.0", jev_mode="replay", **extra))
    return run_id


def log(line_id, attempt, h, criterion, result, period="2026-09", **kw):
    row = dict(close_period=period, line_id=line_id, attempt_number=attempt, commentary_hash=h,
               criterion_id=criterion, result=result)
    row.update(kw)
    return row


def line(line_id, commentary="x"):
    return LineIn(line_id=line_id, entity="E", basis="MoM", account="5000 Hosting", prior=1, current=2,
                  commentary=commentary)


def test_team_roles(store):
    store.upsert_team_member("Reviewer@Example.com", "reviewer", "R")
    member = store.team_member("reviewer@example.com")
    assert member.role == "reviewer" and member.email == "reviewer@example.com"
    store.upsert_team_member("reviewer@example.com", "preparer", "R", active=False)
    assert store.team_member("REVIEWER@example.com").active is False
    assert store.team_member("nobody@example.com") is None
    store.upsert_team_member("b@example.com", "preparer")
    assert [m.email for m in store.team_members()] == ["b@example.com", "reviewer@example.com"]


def test_sheet_keys(store):
    key = SheetKey("fcc_k_1", "hash-1", SHEET, "preparer", "test", 30)
    store.add_sheet_key(key, "admin")
    assert store.sheet_key("hash-1").sheet_id == SHEET
    store.touch_sheet_key("fcc_k_1")
    assert store.revoke_sheet_key("fcc_k_1") is True
    assert store.sheet_key("hash-1").revoked_at is not None
    assert store.revoke_sheet_key("fcc_k_1") is False


def test_lock_is_exclusive_until_released_or_expired(store):
    first = store.acquire_lock(SHEET, str(uuid.uuid4()), "a@example.com", 60)
    second = store.acquire_lock(SHEET, str(uuid.uuid4()), "b@example.com", 60)
    assert first.acquired and not second.acquired
    assert second.holder == "a@example.com" and second.run_id == first.run_id
    assert store.release_lock(SHEET, second.run_id if False else str(uuid.uuid4())) is False
    assert store.release_lock(SHEET, first.run_id) is True
    assert store.acquire_lock(SHEET, str(uuid.uuid4()), "b@example.com", 60).acquired


def test_expired_lock_can_be_taken_over(store):
    stale = store.acquire_lock(SHEET, str(uuid.uuid4()), "a@example.com", 0)
    assert stale.acquired
    import time
    time.sleep(0.05)
    assert store.acquire_lock(SHEET, str(uuid.uuid4()), "b@example.com", 60).acquired


def test_concurrent_acquire_has_exactly_one_winner(store):
    winners = []
    barrier = threading.Barrier(8)

    def contend(i):
        barrier.wait()
        if store.acquire_lock("race-sheet", str(uuid.uuid4()), "user{0}".format(i), 60).acquired:
            winners.append(i)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_evaluation_round_trip_and_prior_attempts(store):
    run1 = new_run(store)
    store.record_evaluation(SHEET, run1, [line("E|2026-09|MoM|5000", "draft")], [
        log("E|2026-09|MoM|5000", 1, commentary_hash("draft"), "C1", "fail", jev_probability=0.2, threshold=0.6,
            criterion_version=1, question_builder_version="qb-1.0.0", model_version="m", calibration_status="Calibrated",
            jev_source="replay", detail={"tier": "jev"}),
        log("E|2026-09|MoM|5000", 1, commentary_hash("draft"), "ARITH", "pass", detail={"tier": "deterministic"}),
    ], {"lines_received": 1, "lines_evaluated": 1, "jev_requests": 1})
    prior = store.prior_attempts(SHEET, ["E|2026-09|MoM|5000", "E|2026-09|MoM|9999"])
    assert prior == {"E|2026-09|MoM|5000": PriorAttempt(1, commentary_hash("draft"), True)}
    rows = store.log_rows(SHEET)
    assert len(rows) == 2 and rows[0]["jev_probability"] == pytest.approx(0.2)
    assert rows[0]["detail"] == {"tier": "jev"}
    assert store.close_periods(SHEET) == ["2026-09"]
    assert store.get_run(run1)["lines_evaluated"] == 1


def test_stale_lines_are_not_delivered_attempts(store):
    lid = "E|2026-09|MoM|5000"
    run1 = new_run(store)
    store.record_evaluation(SHEET, run1, [line(lid, "v1")], [log(lid, 1, commentary_hash("v1"), "C1", "fail")], {})
    store.finish_run(run1, "completed", [], 1)
    run2 = new_run(store)
    store.record_evaluation(SHEET, run2, [line(lid, "v2")], [log(lid, 2, commentary_hash("v2"), "C1", "pass")], {})
    store.finish_run(run2, "completed", [lid], 0)  # edited mid-run: never shown to the preparer
    assert store.prior_attempts(SHEET, [lid])[lid] == PriorAttempt(1, commentary_hash("v1"), True)


def test_blank_rows_do_not_count_as_attempts(store):
    lid = "E|2026-09|MoM|6000"
    run1 = new_run(store)
    store.record_evaluation(SHEET, run1, [line(lid, "")], [log(lid, 0, commentary_hash(""), "COMMENTARY", "missing")],
                            {})
    assert store.prior_attempts(SHEET, [lid]) == {}


def test_log_filters_by_close_and_limits(store):
    run1 = new_run(store)
    store.record_evaluation(SHEET, run1, [], [log("E|2026-08|MoM|1", 1, "h", "C1", "pass", period="2026-08"),
                                              log("E|2026-09|MoM|1", 1, "h", "C1", "pass")], {})
    assert [r["close_period"] for r in store.log_rows(SHEET, ["2026-08"])] == ["2026-08"]
    assert len(store.log_rows(SHEET, limit=1)) == 1
    assert store.log_rows("other-sheet") == []


def test_criteria_versions(store):
    rows = [Criterion(id="C1", passes_when="p", feedback="f1", threshold=0.6), Criterion(id="ARITH", feedback="a")]
    digest, content = criteria_versions.snapshot(rows)
    latest, state = store.criteria_state(SHEET)
    assert latest is None and state == {}
    changes = criteria_versions.changes(state, rows)
    store.record_criteria(SHEET, digest, content, changes, {}, "r@example.com", None)
    latest, state = store.criteria_state(SHEET)
    assert latest == digest and state["C1"][0] == 1

    edited = [rows[0].model_copy(update={"feedback": "f2"}), rows[1]]
    digest2, content2 = criteria_versions.snapshot(edited)
    changes2 = criteria_versions.changes(state, edited)
    assert [(c.criterion_id, c.version, c.fields_changed) for c in changes2] == [("C1", 2, ["Feedback when failed"])]
    assert changes2[0].calibration_impact == "none: Feedback when failed only"
    store.record_criteria(SHEET, digest2, content2, changes2, {"C1": "editor@example.com"}, "r@example.com", None)
    history = store.criteria_history(SHEET)
    assert [(h["criterion_id"], h["version"]) for h in history][-1] == ("C1", 2)
    assert history[-1]["changed_by"] == "editor@example.com"
    assert store.criteria_state(SHEET)[1]["C1"][0] == 2


def test_overrides_are_idempotent(store):
    row = dict(sheet_id=SHEET, close_period="2026-09", line_id="E|2026-09|MoM|1", kind="missed_issue",
               criterion_id=None, not_covered=True, note="hedging?", submitted_by=None, role="reviewer",
               attribution_verified=False, matched_flag=None)
    assert store.add_overrides([row]) == 1
    assert store.add_overrides([row]) == 0
    assert store.add_overrides([dict(row, note="different")]) == 1
    assert len(store.overrides(SHEET, ["2026-09"])) == 2
    assert store.overrides(SHEET, ["2026-08"]) == []


def test_jev_cache(store):
    assert store.jev_cache_get("q:1") is None
    store.jev_cache_put("q:1", "C1", JevAnswer(0.42, "typesafe/jev-1.13-20260917", "live"), {"input_tokens": 10})
    cached = store.jev_cache_get("q:1")
    assert cached.probability == pytest.approx(0.42) and cached.model == "typesafe/jev-1.13-20260917"
    store.jev_cache_put("q:1", "C1", JevAnswer(0.99, "other", "live"), {})  # first answer wins
    assert store.jev_cache_get("q:1").probability == pytest.approx(0.42)


def test_health_rows_and_resolution(store):
    hid = str(uuid.uuid4())
    store.save_health(SHEET, hid, [dict(close_window="2026-09", criterion_id="C5", signal="stale",
                                        evidence={"closes": 3}, suggestion="Nobody is maintaining it")], "r@x.com")
    rows = store.health_rows(SHEET)
    assert len(rows) == 1 and rows[0]["signal"] == "stale" and rows[0]["resolution"] is None
    assert store.resolve_health(SHEET, [{"id": rows[0]["id"], "resolution": "keep"}], "r@x.com") == 1
    assert store.health_rows(SHEET, hid)[0]["resolution"] == "keep"


def test_calibration_records(store):
    store.add_calibration(dict(criterion_id="C1", fingerprint="cal:1", model_snapshot="m",
                               question_builder_version="qb-1.0.0", passes_when_hash="h", threshold=0.6,
                               status="Calibrated", metrics={}, labeled_set_hash="l", label_source="team"))
    assert ("C1", "cal:1", "calibrated") in store.calibration_records()
