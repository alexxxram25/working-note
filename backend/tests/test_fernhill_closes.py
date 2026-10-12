"""Fernhill's three closes, end to end, on recorded real Jev answers.

Every Jev probability used here is a real jev-1.13 answer
(typesafe/jev-1.13-20260917, question builder qb-1.1.0), recorded once and
replayed strictly, so a question with no recording is an error, not a guess.
Each close is worked the way a team would work it, and the engine's
guarantees are checked across every run:

  - no line is ever left ungraded;
  - Jev is never asked about an immaterial line or one that failed an
    objective check, and objective failures reach the preparer first;
  - Criteria Health raises the prompts the history calls for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import fernhill
from coach.api import create_app
from coach.jev.replay import load_recordings
from coach.models import Criterion, LineResult

SHEET = "timeline-test"


@pytest.fixture(scope="module")
def timeline():
    services = fernhill.services()
    client = TestClient(create_app(services=services))
    criteria, settings = fernhill.criteria_rows(), fernhill.settings_rows()
    runs = []

    def runner(lines):
        response = client.post("/v1/runs", json={"sheet_id": SHEET, "criteria": criteria, "settings": settings,
                                                 "lines": [l.model_dump() for l in lines]})
        assert response.status_code == 200, response.text
        body = response.json()
        runs.append(body)
        client.post("/v1/runs/{0}/complete".format(body["run_id"]),
                    json={"sheet_id": SHEET, "written": [r["line_id"] for r in body["results"]]})
        return [LineResult(**r) for r in body["results"]]

    closes = {close: fernhill.simulate_close(close, runner) for close in fernhill.CLOSES}
    return {"closes": closes, "runs": runs, "store": services.store,
            "criteria": [Criterion.from_sheet_row(r) for r in criteria]}


def test_recordings_cover_every_labeled_text_from_one_snapshot():
    recordings = load_recordings(fernhill.RECORDINGS)
    assert len(recordings) == len(fernhill.labels()) * 6
    assert {r.model for r in recordings.values()} == {fernhill.SNAPSHOT}
    assert {r.source for r in recordings.values()} == {"live"}


def test_no_line_is_ever_left_ungraded(timeline):
    for close, rounds in timeline["closes"].items():
        for round_ in rounds:
            errors = [r.line_id for r in round_.results if r.status == "error"]
            assert not errors, (close, round_.number, errors)


def test_jev_only_sees_material_lines_that_passed_the_objective_checks(timeline):
    for run in timeline["runs"]:
        for result in run["results"]:
            asked = any(c["tier"] == "jev" and c["result"] in ("pass", "fail", "prompt") for c in result["checks"])
            objective_fail = any(c["criterion_id"] in ("ARITH", "DUP") and c["result"] == "fail"
                                 for c in result["checks"])
            if not result["material"] or objective_fail or result["status"] == "needs_commentary":
                assert not asked, result["line_id"]
        assert run["summary"]["lines_sent_to_jev"] == sum(
            1 for r in run["results"]
            if any(c["tier"] == "jev" and c["result"] in ("pass", "fail", "prompt") for c in r["checks"]))


def test_objective_failures_reach_the_preparer_before_any_model_call(timeline):
    july = timeline["closes"]["2026-07"]
    by_id = {r.line_id: r for r in july[0].results}
    assert by_id["FHD-US|2026-07|MoM|5200"].failed == ["DUP"]
    assert by_id["FHD-US|2026-07|MoM|6330"].failed == ["DUP"]
    second = {r.line_id: r for r in july[1].results}
    assert second["FHD-US|2026-07|MoM|2100"].failed == ["ARITH"]
    assert {c.result for c in second["FHD-US|2026-07|MoM|2100"].checks if c.tier == "jev"} == {"blocked"}


def test_criteria_health_on_fernhills_full_history(timeline):
    """With every override Fernhill records, September's health check raises real prompts."""
    store = timeline["store"]
    services_app = TestClient(create_app(services=_services_for(store)))
    for close in fernhill.CLOSES:
        overrides = [{"line_id": o["line_id"], "value": o["override"], "note": o["note"]}
                     for o in fernhill.overrides(close) if o["role"] == "reviewer"]
        response = services_app.post("/v1/overrides", json={"sheet_id": SHEET, "overrides": overrides})
        assert response.status_code == 200 and not response.json()["invalid"], response.text
    report = services_app.post("/v1/health", json={"sheet_id": SHEET, "criteria": fernhill.criteria_rows(),
                                                    "settings": fernhill.settings_rows()}).json()
    signals = {(p["criterion_id"], p["signal"]) for p in report["prompts"]}
    expected = {("C5", "stale"), ("NOT COVERED", "not_covered"), ("C4", "flag_wrong"), ("C5", "flag_wrong")}
    assert expected <= signals, signals
    # C1 used to sit on its threshold and draw "flag was wrong" overrides. Its tightened wording
    # (docs/evidence/c1-blind-check) leaves Criteria Health nothing to raise about it.
    assert not any(cid == "C1" for cid, _ in signals), signals
    assert not any(cid in ("ARITH", "DUP", "NEAR") and signal in ("never_fails", "always_fails", "near_threshold")
                   for cid, signal in signals)
    assert report["close_window"] == "2026-09"
    assert report["second_attempts"]["falling"] is True
    print("\nSeptember Criteria Health prompts: " + ", ".join("{0}/{1}".format(*s) for s in sorted(signals)))


def _services_for(store):
    services = fernhill.services()
    services.store = store
    return services
