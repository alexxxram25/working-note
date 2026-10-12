"""The service over HTTP, with a MemoryStore, a fake Jev and real auth."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from coach.api import build_services, create_app
from coach.auth import issue_key
from coach.config import Config
from coach.jev.questions import QUESTION_BUILDER_VERSION, calibration_fingerprint
from coach.models import Criterion
import fernhill
from helpers import AUDIENCE, SNAP, FakeJev, Keys

KEYS = Keys()
SHEET = "sheet-abc"
HOSTING = fernhill.HOSTING_LINE


def make(jev=None, calibrate=True, **overrides):
    config = Config(env="development", store="memory", auth_modes=("google", "sheet_key"),
                    google_audiences=(AUDIENCE,), **overrides)
    services = build_services(config)
    fernhill.seed_team(services.store)
    from coach.auth import GoogleVerifier
    services.auth.google = GoogleVerifier([AUDIENCE], jwks_client=KEYS.jwks_client())
    services.jev = jev or FakeJev()
    services.known_snapshots = (SNAP,)
    if calibrate:
        for row in fernhill.criteria_rows():
            c = Criterion.from_sheet_row(row)
            if c.kind == "jev":
                services.store.add_calibration(dict(
                    criterion_id=c.id, fingerprint=calibration_fingerprint(SNAP, c.passes_when, c.threshold),
                    model_snapshot=SNAP, question_builder_version="qb-1.0.0", passes_when_hash="h",
                    threshold=c.threshold, status="Calibrated", metrics={}, labeled_set_hash="l",
                    label_source="synthetic"))
    return TestClient(create_app(services=services), raise_server_exceptions=False), services


def as_reviewer():
    return {"Authorization": "Bearer " + KEYS.token("reviewer@example.com")}


def as_preparer():
    return {"Authorization": "Bearer " + KEYS.token("preparer@example.com")}


def run_body(texts=None, criteria=None, settings=None, period="2026-09"):
    lines = [l.model_dump() for l in fernhill.lines_for(period, texts or {})]
    return {"sheet_id": SHEET, "criteria": criteria or fernhill.criteria_rows(),
            "settings": settings if settings is not None else fernhill.settings_rows(), "lines": lines}


def start(client, headers=None, **kw):
    return client.post("/v1/runs", json=run_body(**kw), headers=headers or as_preparer())


def complete(client, run_id, headers=None, stale=(), written=()):
    return client.post("/v1/runs/{0}/complete".format(run_id), headers=headers or as_preparer(),
                       json={"sheet_id": SHEET, "stale": list(stale), "written": list(written)})


def result(response, line_id):
    return next(r for r in response.json()["results"] if r["line_id"] == line_id)


def test_healthz_reports_posture_without_secrets():
    client, _ = make(openrouter_api_key="sk-or-planted-secret-123",
                     database_url="postgresql://user:planted-password@db.example.com/x")
    response = client.get("/healthz")
    body = response.json()
    assert body["ok"] and body["store"] == "memory" and body["jev"]["pinned_version"] == "jev-1.13.0"
    assert body["jev"]["request_model"] == "typesafe/jev-1.13-20260917"
    assert body["question_builder_version"] == QUESTION_BUILDER_VERSION
    assert "planted" not in response.text
    assert response.headers["X-Request-ID"]


def test_me_and_defaults():
    client, _ = make()
    assert client.get("/v1/me").status_code == 401
    me = client.get("/v1/me", headers=as_reviewer()).json()
    assert (me["email"], me["role"], me["method"], me["verified"]) == ("reviewer@example.com", "reviewer", "google",
                                                                       True)
    defaults = client.get("/v1/defaults", headers=as_preparer()).json()
    assert [c["ID"] for c in defaults["criteria"]][:5] == ["C1", "C2", "C3", "C4", "C5"]
    # The starter criteria arrive uncalibrated, with starting thresholds and no review date.
    statuses = {c["ID"]: c["Calibration status"] for c in defaults["criteria"]}
    assert statuses == {"C1": "Uncalibrated", "C2": "Uncalibrated", "C3": "Uncalibrated", "C4": "Uncalibrated",
                        "C5": "Uncalibrated", "RESTATE": "Uncalibrated", "ARITH": "Deterministic",
                        "DUP": "Deterministic", "NEAR": "Heuristic"}
    assert not any(c["Last reviewed"] for c in defaults["criteria"])
    assert defaults["settings"][0]["Setting"] == "Materiality ($)"


def test_a_run_grades_logs_and_holds_the_lease_until_complete():
    draft = fernhill.versions("2026-09")[HOSTING][0]
    jev = FakeJev({draft: {"C1": 0.1}})
    client, services = make(jev)
    response = start(client)
    assert response.status_code == 200, response.text
    body = response.json()
    hosting = result(response, HOSTING)
    assert hosting["status"] == "needs_revision" and hosting["failed"] == ["C1"]
    assert hosting["feedback_cell"].startswith("C1: Name the specific vendor")
    assert body["summary"]["skipped_immaterial"] == 13
    # Immaterial lines never reached Jev.
    sent = {c["commentary"] for c in jev.calls}
    immaterial = {r["Commentary"] for r in fernhill.flux_rows("2026-09")} - sent
    assert "Webinar series continues." in immaterial
    assert body["summary"]["lines_sent_to_jev"] == len(jev.calls) == 27

    busy = start(client, headers=as_reviewer())
    assert busy.status_code == 409 and "preparer@example.com" in busy.json()["detail"]

    done = complete(client, body["run_id"], written=[r["line_id"] for r in body["results"]])
    assert done.status_code == 200 and done.json()["released"] is True
    assert start(client, headers=as_reviewer()).status_code == 200
    log = services.store.log_rows(SHEET)
    assert {r["criterion_id"] for r in log} >= {"MATERIALITY", "ARITH", "DUP", "NEAR", "C1", "RESTATE"}


def test_second_attempt_after_a_revision_and_stale_lines_do_not_count():
    versions = fernhill.versions("2026-09")[HOSTING]
    client, _ = make(FakeJev({versions[0]: {"C1": 0.1}}))
    first = start(client)
    assert result(first, HOSTING)["attempt_number"] == 1
    complete(client, first.json()["run_id"])

    rerun = start(client)  # unchanged: same attempt
    assert result(rerun, HOSTING)["attempt_number"] == 1
    complete(client, rerun.json()["run_id"])

    revised = start(client, texts={HOSTING: versions[1]})
    assert result(revised, HOSTING)["attempt_number"] == 2
    assert result(revised, HOSTING)["status"] == "passed"
    # The preparer edited the line while this run was in flight: its results were never shown.
    complete(client, revised.json()["run_id"], stale=[HOSTING])

    again = start(client, texts={HOSTING: versions[1] + " Confirmed with AP."})
    assert result(again, HOSTING)["attempt_number"] == 2


def test_only_the_starter_can_complete_and_unknown_runs_404():
    client, _ = make()
    run_id = start(client).json()["run_id"]
    assert complete(client, run_id, headers=as_reviewer()).status_code == 403
    assert complete(client, "00000000-0000-0000-0000-000000000000").status_code == 404
    assert complete(client, "not-a-uuid").status_code == 404
    assert complete(client, run_id).status_code == 200


def test_settings_pin_mismatch_is_refused_not_switched():
    client, _ = make()
    settings = [dict(r) for r in fernhill.settings_rows()]
    for row in settings:
        if row["Setting"] == "Pinned model version":
            row["Value"] = "jev-1.14.0"
    response = start(client, settings=settings)
    assert response.status_code == 409 and "pinned to jev-1.13.0" in response.json()["detail"]


def test_bad_criteria_and_settings_are_422_with_reasons():
    client, _ = make()
    rows = [dict(r) for r in fernhill.criteria_rows()]
    rows[0]["Threshold"] = "1.7"
    rows[1]["ID"] = "C1"
    response = start(client, criteria=rows)
    assert response.status_code == 422
    problems = response.json()["detail"]["problems"]
    assert any("Criteria row 2" in p for p in problems) or any("more than once" in p for p in problems)
    rows = [dict(r) for r in fernhill.criteria_rows()]
    rows[0]["Threshold"] = 0.9125
    response = start(client, criteria=rows)
    assert response.status_code == 422
    assert any("Criteria row 2" in p and "at most three decimals" in p for p in response.json()["detail"]["problems"])
    bad_settings = [{"Setting": "Minimum sample size", "Value": "lots"}]
    assert start(client, settings=bad_settings).status_code == 422


def test_nonfinite_numbers_are_422_not_500():
    client, _ = make()
    # JSON parsers accept NaN and Infinity tokens; the service must not.
    body = run_body()
    body["lines"][0]["prior"] = float("nan")
    response = client.post("/v1/runs", content=json.dumps(body), headers=dict(as_preparer(),
                                                                                **{"Content-Type": "application/json"}))
    assert response.status_code == 422, response.text
    assert "finite" in response.text
    infinite = [{"Setting": "Minimum sample size", "Value": "Infinity"}]
    response = start(client, settings=infinite)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["message"] == "Fix the Settings tab"
    # An integer literal too large for a float parses as an int; float() of it is an
    # OverflowError, which pydantic does not turn into a validation error on its own.
    body = run_body()
    body["lines"][0]["prior"] = 10 ** 400
    response = client.post("/v1/runs", json=body, headers=as_preparer())
    assert response.status_code == 422, response.text
    assert "finite" in response.text
    huge = [{"Setting": "Minimum sample size", "Value": 10 ** 400}]
    assert start(client, settings=huge).status_code == 422


def test_results_of_a_run_whose_lease_was_taken_over_are_discarded():
    from coach.store.base import utcnow
    import datetime as dt
    import uuid

    client, services = make()
    store = services.store
    intruder = str(uuid.uuid4())

    class Stealing(FakeJev):
        def evaluate(self, state, questions, session_id=None):
            if not self.calls:
                # The lease lapses mid-run and another run takes the sheet.
                run_id, holder, _ = store._locks[SHEET]
                store._locks[SHEET] = (run_id, holder, utcnow() - dt.timedelta(seconds=1))
                assert store.acquire_lock(SHEET, intruder, "intruder@example.com", 60).acquired
            return super().evaluate(state, questions, session_id)

    services.jev = Stealing()
    response = start(client)
    assert response.status_code == 409, response.text
    assert "discarded" in response.json()["detail"]
    assert store.log_rows(SHEET) == [], "nothing from the superseded run reached the Log"
    runs = [r for r in store._runs.values() if r["sheet_id"] == SHEET]
    assert len(runs) == 1 and runs[0]["status"] == "failed" and runs[0]["error"] == "LeaseLost"
    # The intruder's lease is untouched: the loser did not free a lease it did not hold.
    assert not store.acquire_lock(SHEET, str(uuid.uuid4()), "x@example.com", 60).acquired
    assert store.release_lock(SHEET, intruder)
    services.jev = FakeJev()
    assert start(client).status_code == 200


def test_the_lease_heartbeat_renews_at_most_every_third_of_the_ttl_and_reports_a_lost_lease(monkeypatch):
    from coach.api import _lease_heartbeat
    from coach.store.base import LeaseLost
    from coach import api as api_module

    clock = [1000.0]
    monkeypatch.setattr(api_module.time, "monotonic", lambda: clock[0])
    renewals = []

    class Renewing:
        def renew_lock(self, sheet_id, run_id, ttl):
            renewals.append((sheet_id, run_id, ttl))
            return len(renewals) < 3

    beat = _lease_heartbeat(Renewing(), "s", "r", 180)
    for _ in range(5):
        beat()
    assert renewals == [], "nothing to renew within the first third of the TTL"
    clock[0] += 61
    beat()
    assert renewals == [("s", "r", 180)]
    clock[0] += 61
    beat()
    clock[0] += 61
    with pytest.raises(LeaseLost):
        beat()
    assert len(renewals) == 3


def test_a_repeated_line_id_is_rejected_in_every_occurrence_and_never_logged():
    client, services = make()
    body = run_body()
    hosting = next(l for l in body["lines"] if l["line_id"] == HOSTING)
    body["lines"].append(dict(hosting, commentary="Hosting rose because of something else entirely.", row=99))
    response = client.post("/v1/runs", json=body, headers=as_preparer())
    assert response.status_code == 200, response.text
    out = response.json()
    assert [r["line_id"] for r in out["rejected"]] == [HOSTING, HOSTING]
    assert all("appears 2 times" in r["error"] for r in out["rejected"])
    assert not any(r["line_id"] == HOSTING for r in out["results"])
    assert out["summary"]["rejected"] == 2 and out["summary"]["lines"] == len(body["lines"]) - 2
    assert not any(r["line_id"] == HOSTING for r in services.store.log_rows(SHEET))
    assert services.store.prior_attempts(SHEET, [HOSTING]) == {}


def test_a_run_with_every_criterion_switched_off_is_refused_not_passed():
    client, services = make()
    rows = [dict(r, **{"Active (Y/N)": "N"}) for r in fernhill.criteria_rows()]
    response = start(client, criteria=rows)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["message"] == "The Criteria tab has no active criteria"
    assert any("Set at least one criterion to Y" in p for p in detail["problems"])
    assert services.store.log_rows(SHEET) == []
    # The lease was never taken: the next run starts at once.
    assert start(client).status_code == 200


def test_criteria_edits_are_versioned_with_their_calibration_impact():
    client, _ = make()
    first = start(client).json()
    complete(client, first["run_id"])
    assert {c["calibration_impact"].split(":")[0] for c in first["criteria_changes"]} == {"new criterion"}
    rows = [dict(r) for r in fernhill.criteria_rows()]
    rows[0]["Feedback when failed"] = "Who or what, specifically?"
    second = start(client, criteria=rows).json()
    change = second["criteria_changes"][0]
    assert (change["criterion_id"], change["version"], change["calibration_impact"]) == (
        "C1", 2, "none: Feedback when failed only")
    status = {c["id"]: c for c in second["criteria_status"]}
    assert status["C1"]["calibration_status"] == "Calibrated" and status["C1"]["version"] == 2
    complete(client, second["run_id"])
    rows[0]["Passes when"] = "Names the customer"
    third = start(client, criteria=rows).json()
    assert third["criteria_changes"][0]["calibration_impact"] == "uncalibrated: Passes when changed"
    assert {c["id"]: c for c in third["criteria_status"]}["C1"]["calibration_status"] == "Uncalibrated"


def test_reviewer_only_routes_refuse_preparers():
    client, _ = make()
    body = {"sheet_id": SHEET, "criteria": fernhill.criteria_rows()}
    assert client.post("/v1/criteria/status", json=body, headers=as_preparer()).status_code == 403
    assert client.post("/v1/health", json=body, headers=as_preparer()).status_code == 403
    assert client.get("/v1/team", headers=as_preparer()).status_code == 403
    assert client.post("/v1/criteria/status", json=body, headers=as_reviewer()).status_code == 200


def test_the_service_has_no_demo_routes():
    client, services = make()
    app = create_app(services=services)
    paths = sorted(getattr(r, "path", "") for r in app.routes)
    assert not [p for p in paths if "demo" in p], paths
    assert "/v1/runs" in paths  # the probe reads real routes
    assert client.get("/v1/demo/closes/2026-09", headers=as_reviewer()).status_code == 404
    assert client.post("/v1/demo/history", json={"sheet_id": SHEET}, headers=as_reviewer()).status_code == 404


def test_sheet_keys_are_scoped_to_their_sheet():
    client, services = make()
    raw, record = issue_key(SHEET, "preparer")
    services.store.add_sheet_key(record)
    headers = {"X-Coach-Sheet-Key": raw, "X-Coach-User": "preparer@example.com"}
    assert client.get("/v1/me", headers=headers).json()["role"] == "preparer"
    other = run_body()
    other["sheet_id"] = "someone-elses-sheet"
    assert client.post("/v1/runs", json=other, headers=headers).status_code == 403


def test_body_cap_and_rate_limit():
    client, _ = make(max_body_bytes=2000)
    assert start(client).status_code == 413
    client, _ = make(rate_limit_per_minute=2)
    assert client.get("/v1/me", headers=as_reviewer()).status_code == 200
    assert client.get("/v1/me", headers=as_reviewer()).status_code == 200
    assert client.get("/v1/me", headers=as_reviewer()).status_code == 429


def test_a_crash_mid_run_releases_the_lease_and_returns_a_request_id():
    draft = fernhill.versions("2026-09")[HOSTING][0]
    client, _ = make(FakeJev(explode_on=draft))
    crashed = start(client)
    assert crashed.status_code == 500
    assert crashed.json()["request_id"] == crashed.headers["X-Request-ID"]
    assert "unexpected bug" not in crashed.text
    client.app.state.services.jev = FakeJev()
    assert start(client).status_code == 200


def test_overrides_are_attributed_idempotent_and_validated():
    draft = fernhill.versions("2026-09")[HOSTING][0]
    client, services = make(FakeJev({draft: {"C1": 0.1}}))
    run_id = start(client).json()["run_id"]
    complete(client, run_id)
    body = {"sheet_id": SHEET, "overrides": [
        {"line_id": HOSTING, "value": "Flag was wrong: C1", "note": "Large customer is fine here"},
        {"line_id": HOSTING, "value": "Flag was wrong: C4", "note": "C4 never flagged"},
        {"line_id": "FHD-US|2026-09|MoM|7100", "value": "Missed an issue: not covered", "note": "Hedging?"},
        {"line_id": HOSTING, "value": "Flag was wrong: not covered"},
        {"line_id": "bad id", "value": "Missed an issue: C2"},
        {"line_id": HOSTING, "value": "Looks fine"},
    ]}
    first = client.post("/v1/overrides", json=body, headers=as_reviewer()).json()
    assert first["recorded"] == 3 and len(first["invalid"]) == 3
    assert first["attributed_to"] == "reviewer@example.com" and first["attribution_verified"] is True
    again = client.post("/v1/overrides", json=body, headers=as_reviewer()).json()
    assert again["recorded"] == 0 and again["already_recorded"] == 3
    stored = {(o["kind"], o.get("criterion_id")): o for o in services.store.overrides(SHEET)}
    assert stored[("flag_wrong", "C1")]["matched_flag"] is True
    assert stored[("flag_wrong", "C4")]["matched_flag"] is False
    assert stored[("missed_issue", None)]["not_covered"] is True


def test_preparer_without_email_overrides_without_attribution():
    client, _ = make()
    token = {"Authorization": "Bearer " + KEYS.token(email=None)}
    body = {"sheet_id": SHEET, "overrides": [{"line_id": HOSTING, "value": "Missed an issue: C3"}]}
    response = client.post("/v1/overrides", json=body, headers=token).json()
    assert response["attributed_to"] is None and response["recorded"] == 1


def test_uncalibrated_service_routes_flags_as_prompts():
    draft = fernhill.versions("2026-09")[HOSTING][0]
    client, _ = make(FakeJev({draft: {"C1": 0.1}}), calibrate=False)
    line = result(start(client), HOSTING)
    assert line["status"] == "reviewer_prompt" and line["prompts"] == ["C1"] and line["feedback_cell"] == ""


def test_jev_errors_leave_lines_ungraded():
    draft = fernhill.versions("2026-09")[HOSTING][0]
    client, _ = make(FakeJev(fail_on=draft))
    line = result(start(client), HOSTING)
    assert line["status"] == "error" and line["status_label"] == "Not graded"


def test_unsafe_production_config_is_refused():
    with pytest.raises(Exception) as raised:
        build_services(Config(env="production", store="memory", auth_modes=("google", "local"),
                              google_audiences=(), jev_model="~typesafe/jev-latest"))
    message = str(raised.value)
    for fragment in ("alias", "GOOGLE_AUDIENCES", "in-memory store", "local (no-credential) auth"):
        assert fragment in message


def test_health_endpoint_reports_prompts_history_and_log_and_records_resolutions():
    draft = fernhill.versions("2026-09")[HOSTING][0]
    client, services = make(FakeJev({draft: {"C1": 0.1}}))
    for period in ("2026-08", "2026-09"):  # two closes: staleness counts closes since the last review
        body = start(client, period=period).json()
        complete(client, body["run_id"])
    services.store.add_overrides([dict(
        sheet_id=SHEET, close_period="2026-09", line_id=HOSTING, kind="missed_issue", criterion_id=None,
        not_covered=True, note="Hedging?", submitted_by="reviewer@example.com", role="reviewer",
        attribution_verified=True, matched_flag=None)] * 1 + [dict(
        sheet_id=SHEET, close_period="2026-09", line_id="FHD-US|2026-09|MoM|7100", kind="missed_issue",
        criterion_id=None, not_covered=True, note="Forecast tie-out", submitted_by="reviewer@example.com",
        role="reviewer", attribution_verified=True, matched_flag=None)])
    request = {"sheet_id": SHEET, "criteria": fernhill.criteria_rows(), "settings": fernhill.settings_rows(),
               "include_log": True, "log_limit": 50}
    report = client.post("/v1/health", json=request, headers=as_reviewer()).json()
    signals = {(p["criterion_id"], p["signal"]) for p in report["prompts"]}
    assert ("C5", "stale") in signals and ("NOT COVERED", "not_covered") in signals
    assert all(isinstance(p["id"], int) for p in report["prompts"])
    assert report["close_window"] == "2026-09"
    assert len(report["log"]) == 50 and report["log"][0]["criterion_id"]
    assert {h["criterion_id"] for h in report["history"]} >= {"C1", "C5"}
    assert report["second_attempts"]["closes"] == ["2026-08", "2026-09"]
    stale = next(p for p in report["prompts"] if p["signal"] == "stale")
    request["resolutions"] = [{"id": stale["id"], "resolution": "keep"}]
    again = client.post("/v1/health", json=request, headers=as_reviewer()).json()
    assert again["resolutions_recorded"] == 1
    bad = dict(request, resolutions=[{"id": stale["id"], "resolution": "ignore"}])
    assert client.post("/v1/health", json=bad, headers=as_reviewer()).status_code == 422


def test_team_list_is_for_reviewers():
    client, _ = make()
    assert client.get("/v1/team", headers=as_preparer()).status_code == 403
    team = client.get("/v1/team", headers=as_reviewer()).json()
    assert team == {"reviewers": ["reviewer@example.com"], "preparers": ["preparer@example.com"]}


def test_logging_sends_info_to_stdout_and_errors_to_stderr():
    import logging
    import sys

    from coach.api import configure_logging

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        out, err = configure_logging()
        info = logging.LogRecord("coach", logging.INFO, __file__, 1, "request", None, None)
        error = logging.LogRecord("coach", logging.ERROR, __file__, 1, "boom", None, None)
        assert out.filter(info) and not out.filter(error)
        assert info.levelno < err.level <= error.levelno
        assert out.stream is sys.stdout and err.stream is sys.stderr
    finally:
        root.handlers[:], _ = saved
        root.setLevel(saved[1])
