"""Criteria Health signals and the second-attempts view: each fires, and each stays quiet."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from coach import second_attempts
from coach.health import NOT_COVERED_ID, SUGGESTIONS, health_report
from coach.models import Criterion, Settings
from coach.store.memory import MemoryStore

SHEET = "health"
SETTINGS = Settings(min_sample_size=20)


def criteria(**overrides):
    rows = [
        Criterion(id="C1", name="Names a specific driver", passes_when="p1", threshold=0.6, last_reviewed="2026-09-08"),
        Criterion(id="C2", name="Explains why", passes_when="p2", threshold=0.5, last_reviewed="2026-09-08"),
        Criterion(id="ARITH", name="Amounts add up", last_reviewed="2026-09-08"),
    ]
    return [c.model_copy(update=overrides.get(c.id, {})) for c in rows]


def record(store, close, rows, attempt=1):
    """rows: list of (line_number, criterion_id, result, probability, threshold)."""
    run_id = str(uuid.uuid4())
    store.create_run(dict(run_id=run_id, sheet_id=SHEET, role="preparer", auth_method="local",
                          attribution_verified=False, settings={}, question_builder_version="qb",
                          model_pin="jev-1.13.0", jev_mode="replay"))
    log = []
    for number, cid, result, p, t in rows:
        log.append(dict(close_period=close, line_id="E|{0}|MoM|{1}".format(close, number), attempt_number=attempt,
                        commentary_hash="h{0}-{1}".format(number, attempt), criterion_id=cid, result=result,
                        jev_probability=p, threshold=t))
    store.record_evaluation(SHEET, run_id, [], log, {})
    return run_id


def lines(n, cid, fails=0, p_pass=0.95, p_fail=0.1, threshold=0.6, start=0):
    out = []
    for i in range(n):
        failed = i < fails
        out.append((start + i, cid, "fail" if failed else "pass", p_fail if failed else p_pass, threshold))
    return out


def signals(report, criterion_id=None):
    return {(p["criterion_id"], p["signal"]) for p in report["prompts"]
            if criterion_id is None or p["criterion_id"] == criterion_id}


@pytest.fixture
def store():
    return MemoryStore()


def test_nothing_fires_below_the_minimum_sample(store):
    record(store, "2026-09", lines(10, "C1", fails=10))
    report = health_report(store, SHEET, criteria(), SETTINGS)
    assert report["prompts"] == []
    assert {"criterion_id": "C1", "material_lines": 10, "needed": 20} in report["gated"]


def test_never_fails_fires_and_stays_quiet(store):
    record(store, "2026-09", lines(25, "C2", fails=0) + lines(25, "C1", fails=5))
    report = health_report(store, SHEET, criteria(), SETTINGS)
    assert ("C2", "never_fails") in signals(report)
    assert ("C1", "never_fails") not in signals(report)
    prompt = next(p for p in report["prompts"] if p["signal"] == "never_fails")
    assert prompt["suggestion"] == SUGGESTIONS["never_fails"] == "Team has learned it (retire it) or it's too loose"
    assert "failed 0 of 25 first attempts" in prompt["evidence"]["text"]


def test_always_fails_fires_at_the_threshold(store):
    record(store, "2026-09", lines(20, "C1", fails=12) + lines(20, "C2", fails=11))
    report = health_report(store, SHEET, criteria(), SETTINGS)
    assert ("C1", "always_fails") in signals(report)  # 60%
    assert ("C2", "always_fails") not in signals(report)  # 55%


def test_uncalibrated_prompts_count_as_flags(store):
    rows = [(i, "C1", "prompt", 0.2, 0.6) for i in range(15)] + lines(10, "C1", start=100)
    record(store, "2026-09", rows)
    assert ("C1", "always_fails") in signals(health_report(store, SHEET, criteria(), SETTINGS))


def test_probabilities_clustered_near_the_threshold(store):
    near = [(i, "C1", "pass" if i % 2 else "fail", 0.62 if i % 2 else 0.55, 0.6) for i in range(12)]
    far = lines(12, "C1", fails=2, start=50)
    record(store, "2026-09", near + far + lines(24, "C2", fails=3, p_pass=0.99, p_fail=0.01, threshold=0.5))
    report = health_report(store, SHEET, criteria(), SETTINGS)
    assert ("C1", "near_threshold") in signals(report)
    assert ("C2", "near_threshold") not in signals(report)


def test_guards_get_no_log_rate_prompts(store):
    record(store, "2026-09", [(i, "ARITH", "pass", None, None) for i in range(25)])
    report = health_report(store, SHEET, criteria(), SETTINGS)
    assert not any(cid == "ARITH" for cid, _ in signals(report))
    record(store, "2026-09", [(i, "ARITH", "fail", None, None) for i in range(100, 125)])
    assert not any(cid == "ARITH" for cid, _ in signals(health_report(store, SHEET, criteria(), SETTINGS)))


def test_guards_still_get_override_and_staleness_prompts(store):
    record(store, "2026-07", [(i, "ARITH", "pass", None, None) for i in range(25)])
    record(store, "2026-09", [(i, "ARITH", "pass", None, None) for i in range(25)])
    store.add_overrides([override("2026-09", 1, "flag_wrong", "ARITH", "reviewer"),
                         override("2026-09", 2, "flag_wrong", "ARITH", "reviewer")])
    report = health_report(store, SHEET, criteria(ARITH={"last_reviewed": "2026-01-01"}), SETTINGS)
    assert {("ARITH", "flag_wrong"), ("ARITH", "stale")} <= signals(report)


def override(close, n, kind, cid, role, matched=True, not_covered=False, note="n"):
    return dict(sheet_id=SHEET, close_period=close, line_id="E|{0}|MoM|{1}".format(close, n), kind=kind,
                criterion_id=cid, not_covered=not_covered, note=note, submitted_by=None, role=role,
                attribution_verified=False, matched_flag=matched if kind == "flag_wrong" else None)


def test_flag_wrong_weighs_reviewers_more_and_ignores_unmatched_flags(store):
    record(store, "2026-09", lines(25, "C1", fails=5) + lines(25, "C2", fails=5))
    store.add_overrides([override("2026-09", 1, "flag_wrong", "C1", "reviewer"),
                         override("2026-09", 2, "flag_wrong", "C1", "reviewer"),
                         override("2026-09", 3, "flag_wrong", "C2", "preparer"),
                         override("2026-09", 4, "flag_wrong", "C2", "preparer"),
                         override("2026-09", 5, "flag_wrong", "C2", "reviewer", matched=False)])
    report = health_report(store, SHEET, criteria(), SETTINGS)
    assert ("C1", "flag_wrong") in signals(report)  # 2 + 2 = 4
    assert ("C2", "flag_wrong") not in signals(report)  # 1 + 1, and the unmatched one is ignored


def test_missed_issue_and_not_covered(store):
    record(store, "2026-09", lines(25, "C1", fails=5) + lines(25, "C2", fails=5))
    store.add_overrides([override("2026-09", 1, "missed_issue", "C2", "reviewer"),
                         override("2026-09", 2, "missed_issue", "C2", "preparer"),
                         override("2026-09", 3, "missed_issue", None, "reviewer", not_covered=True, note="Hedging?"),
                         override("2026-09", 4, "missed_issue", None, "reviewer", not_covered=True, note="Forecast")])
    report = health_report(store, SHEET, criteria(), SETTINGS)
    assert ("C2", "missed_issue") in signals(report)
    gap = next(p for p in report["prompts"] if p["criterion_id"] == NOT_COVERED_ID)
    assert gap["signal"] == "not_covered" and gap["suggestion"] == "A standard exists that no criterion covers"
    assert "Hedging?" in gap["evidence"]["text"]


def test_staleness_counts_closes_since_the_last_review(store):
    for close in ("2026-07", "2026-08", "2026-09"):
        record(store, close, lines(25, "C1", fails=5) + lines(25, "C2", fails=5))
    stale = criteria(C1={"last_reviewed": "2026-06-12"}, C2={"last_reviewed": "2026-09-08"})
    report = health_report(store, SHEET, stale, SETTINGS)
    assert ("C1", "stale") in signals(report)
    assert ("C2", "stale") not in signals(report)
    never = criteria(C2={"last_reviewed": None})
    assert ("C2", "stale") in signals(health_report(store, SHEET, never, SETTINGS))


def test_staleness_needs_enough_closes_and_monthly_periods(store):
    record(store, "2026-09", lines(25, "C1", fails=5))
    assert not any(s == "stale" for _, s in signals(health_report(store, SHEET, criteria(
        C1={"last_reviewed": "2020-01-01"}), SETTINGS)))
    other = MemoryStore()
    record(other, "FY26-Q1", lines(25, "C1", fails=5))
    record(other, "FY26-Q2", lines(25, "C1", fails=5))
    assert not any(s == "stale" for _, s in signals(health_report(other, SHEET, criteria(
        C1={"last_reviewed": "2020-01-01"}), SETTINGS)))


def test_rates_read_only_the_window(store):
    record(store, "2026-08", lines(25, "C2", fails=0))
    record(store, "2026-09", lines(25, "C2", fails=8))
    report = health_report(store, SHEET, criteria(), SETTINGS)
    assert ("C2", "never_fails") not in signals(report)
    assert report["close_window"] == "2026-09"
    wide = health_report(store, SHEET, criteria(), SETTINGS.model_copy(update={"health_window_closes": 2}))
    assert wide["close_window"] == "2026-08 to 2026-09"


def test_only_first_attempts_count_toward_fail_rates(store):
    record(store, "2026-09", lines(25, "C2", fails=0))
    record(store, "2026-09", lines(25, "C2", fails=25), attempt=2)
    assert ("C2", "never_fails") in signals(health_report(store, SHEET, criteria(), SETTINGS))


def test_inactive_criteria_are_left_alone(store):
    record(store, "2026-09", lines(25, "C2", fails=0))
    report = health_report(store, SHEET, criteria(C2={"active": False}), SETTINGS)
    assert ("C2", "never_fails") not in signals(report)


def test_empty_sheet_reports_nothing(store):
    report = health_report(store, SHEET, criteria(), SETTINGS)
    assert report["prompts"] == [] and report["material_lines"] == 0


# --- second attempts -------------------------------------------------------------------


def row(close, line, attempt, cid, result):
    return {"close_period": close, "line_id": line, "attempt_number": attempt, "criterion_id": cid, "result": result}


def test_second_attempts_count_lines_that_came_back_after_failing():
    rows = [
        row("2026-08", "A", 1, "C1", "fail"), row("2026-08", "A", 1, "C2", "fail"), row("2026-08", "A", 2, "C1", "pass"),
        row("2026-08", "B", 1, "C1", "fail"),                                   # never came back
        row("2026-08", "C", 1, "C1", "pass"), row("2026-08", "C", 1, "C1", "pass"),  # rerun, same attempt
        row("2026-08", "D", 0, "COMMENTARY", "missing"), row("2026-08", "D", 1, "C1", "pass"),
        row("2026-09", "E", 1, "C1", "fail"), row("2026-09", "E", 2, "C1", "fail"), row("2026-09", "E", 3, "C1", "pass"),
    ]
    crit = [Criterion(id="C1", name="one"), Criterion(id="C2", name="two")]
    table = second_attempts.compute(rows, crit)
    assert table["closes"] == ["2026-08", "2026-09"]
    by_id = {r["criterion_id"]: r for r in table["rows"]}
    assert by_id["C1"]["counts"] == {"2026-08": 1, "2026-09": 1}
    assert by_id["C2"]["counts"] == {"2026-08": 1, "2026-09": 0}
    assert by_id["C1"]["first_attempt_lines"] == {"2026-08": 4, "2026-09": 1}
    assert by_id["C1"]["rates"]["2026-08"] == pytest.approx(0.25)
    assert by_id["C2"]["rates"]["2026-09"] is None
    assert table["totals"] == {"2026-08": 1, "2026-09": 1}
    assert table["falling"] is False
    assert "counts revisions, not learning" in table["caveat"]


def test_falling_requires_a_strict_decline():
    rows = [row("07", "A", 1, "C1", "fail"), row("07", "A", 2, "C1", "pass"),
            row("07", "B", 1, "C1", "fail"), row("07", "B", 2, "C1", "pass"),
            row("08", "C", 1, "C1", "fail"), row("08", "C", 2, "C1", "pass")]
    table = second_attempts.compute(rows, [Criterion(id="C1")], ["07", "08", "09"])
    assert table["totals"] == {"07": 2, "08": 1, "09": 0}
    assert table["falling"] is True
