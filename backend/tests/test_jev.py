"""Jev integration, offline: question builder, clients, replay, cache, thresholds.

Live calls are faked with httpx.MockTransport using the exact response shapes
documented by OpenRouter (Decisions API) and TypeSafe (System One).
"""

from __future__ import annotations

import json

import httpx
import pytest

from coach.jev import client as jc
from coach.jev.client import (JevBadResponse, JevConfigError, JevError, JevModelDrift, JevReplayMiss,
                              LiveJevClient, family_of, validate_request_model)
from coach.jev.questions import (FALSE_CRITERION, QUESTION_BUILDER_VERSION, build_question, build_questions,
                                 build_state, calibration_fingerprint, question_fingerprint)
from coach.jev.replay import (CachingJevClient, MemoryJevCache, Recording, ReplayJevClient, load_recordings,
                              recordings_from_response, write_recordings)
from coach.jev.thresholds import CalibrationIndex, grade
from coach.models import Criterion, LineIn
from conftest import ROOT

C1 = Criterion(id="C1", name="Names a specific driver",
               passes_when='Points to a vendor, contract, hire, customer, or event, not a category like "timing" or "growth"',
               feedback="Name the specific vendor, contract, hire, customer, or event that drove this movement.",
               why="A category can't be checked; a driver can", example="Orion Freight", threshold=0.6)
LINE = LineIn(line_id="FHD-US|2026-09|MoM|5000", account="5000 Cloud hosting", basis="MoM", prior=409000,
              current=457500, commentary="Hosting rose $48.5K because a large customer onboarded.")
OR_MODEL = "typesafe/jev-1.13"
SNAPSHOT = "typesafe/jev-1.13-20260917"

# --- question builder --------------------------------------------------------------


def test_golden_question_and_state():
    """Changing what is sent to Jev must come with a QUESTION_BUILDER_VERSION bump."""
    golden = json.loads((ROOT / "backend" / "tests" / "fixtures" / "question_builder_golden.json").read_text())
    assert golden["version"] == QUESTION_BUILDER_VERSION
    assert build_question(C1) == golden["question"]
    assert build_state(LINE, 48500, 48500 / 409000) == golden["state"]


def test_only_the_pass_condition_reaches_the_question():
    edited = C1.model_copy(update={
        "name": "Different name", "feedback": "Who, specifically?", "why": "New rationale",
        "example": "New example", "owner": "Someone else", "last_reviewed": "2026-10-01",
    })
    assert build_question(edited) == build_question(C1)
    changed = C1.model_copy(update={"passes_when": "Names the customer"})
    assert build_question(changed) != build_question(C1)


def test_every_noul_carries_true_and_false_criteria():
    question = build_question(C1)
    assert question["type"] == "noul"
    assert question["criteria"]["true"].startswith("Points to a vendor")
    assert question["criteria"]["false"] == FALSE_CRITERION


def test_state_is_small_and_has_no_identifiers():
    state = build_state(LINE, 48500, 0.1186)
    assert set(state) == {"row", "commentary"}
    assert set(state["row"]) == {"account", "comparison", "prior", "current", "change"}
    assert "FHD-US" not in json.dumps(state)
    assert state["row"]["comparison"] == "month over month"
    assert state["row"]["change"] == "+$48,500 (+11.9%)"
    zero = build_state(LineIn(line_id="x", account="a", prior=0, current=5), 5, None)
    assert zero["row"]["change"] == "+$5 (prior was zero)"


def test_layout_edits_do_not_change_the_fingerprint():
    a = build_state(LINE, 48500, 0.1186)
    b = build_state(LINE.model_copy(update={"commentary": "  Hosting rose $48.5K   because a large customer onboarded. "}),
                    48500, 0.1186)
    q = build_question(C1)
    assert question_fingerprint(OR_MODEL, a, q) == question_fingerprint(OR_MODEL, b, q)
    assert question_fingerprint(OR_MODEL, a, q) != question_fingerprint("jev-1.13.0", a, q)


def test_calibration_fingerprint_moves_with_its_four_inputs_only():
    base = calibration_fingerprint(SNAPSHOT, C1.passes_when, 0.6)
    assert calibration_fingerprint(SNAPSHOT, C1.passes_when + "  ", 0.6) == base
    assert calibration_fingerprint("typesafe/jev-1.13-20261101", C1.passes_when, 0.6) != base
    assert calibration_fingerprint(SNAPSHOT, "Names the customer", 0.6) != base
    assert calibration_fingerprint(SNAPSHOT, C1.passes_when, 0.61) != base
    assert calibration_fingerprint(SNAPSHOT, C1.passes_when, 0.6, builder_version="qb-9") != base


# --- model pinning ---------------------------------------------------------------------


@pytest.mark.parametrize("provider,model", [
    ("openrouter", "~typesafe/jev-latest"), ("openrouter", "typesafe/jev-latest"),
    ("typesafe", "jev-latest"), ("typesafe", "jev-preview"), ("typesafe", "jev"),
    ("openrouter", "jev-1.13.0"), ("typesafe", "typesafe/jev-1.13"), ("nope", "jev-1.13.0"),
])
def test_aliases_and_wrong_shapes_are_refused(provider, model):
    with pytest.raises(JevConfigError):
        validate_request_model(provider, model)


@pytest.mark.parametrize("provider,model", [
    ("openrouter", "typesafe/jev-1.13"), ("openrouter", "typesafe/jev-1.13-20260917"),
    ("typesafe", "jev-1.13.0"),
])
def test_pinned_versions_are_accepted(provider, model):
    assert validate_request_model(provider, model) == model


def test_family():
    assert family_of(SNAPSHOT) == family_of(OR_MODEL) == "typesafe/jev-1.13"
    assert family_of("jev-1.13.0") == "jev-1.13"


# --- live client against a fake transport ----------------------------------------------


def openrouter_reply(answers, model=SNAPSHOT):
    return {
        "id": "gen-dec-123", "model": model, "provider": "TypeSafe",
        "answers": {qid: {"type": "noul", "noul": p} for qid, p in answers.items()},
        "usage": {"input_tokens": 400, "output_tokens": 30, "cost": 0.0000168},
    }


def make_client(handler, provider="openrouter", model=OR_MODEL, allowed=(SNAPSHOT,), **kw):
    sleeps = []
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = LiveJevClient(provider, "sk-test-secret", model, allowed, http=http, sleep=sleeps.append,
                           max_retries=kw.pop("max_retries", 3), **kw)
    return client, sleeps


def test_openrouter_wire_format_and_parse():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=openrouter_reply({"C1": 0.21}))

    client, _ = make_client(handler)
    response = client.evaluate({"row": {}, "commentary": "x"}, build_questions([C1]), session_id="run-1")
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["auth"] == "Bearer sk-test-secret"
    assert seen["body"]["model"] == OR_MODEL
    assert seen["body"]["provider"] == {"allow_fallbacks": False, "data_collection": "deny"}
    assert seen["body"]["session_id"] == "run-1"
    assert seen["body"]["questions"]["C1"]["criteria"]["false"] == FALSE_CRITERION
    assert response.answers["C1"].probability == 0.21
    assert response.answers["C1"].model == SNAPSHOT
    assert response.answers["C1"].source == "live"
    assert response.requests == 1


def test_typesafe_direct_wire_format():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "jev-1.13.0",
                                         "answers": {"C1": {"type": "noul", "noul": 0.95}},
                                         "usage": {"input_tokens": 296, "output_tokens": 20}})

    client, _ = make_client(handler, provider="typesafe", model="jev-1.13.0", allowed=("jev-1.13.0",))
    response = client.evaluate({"commentary": "x"}, build_questions([C1]))
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert "provider" not in seen["body"]
    assert response.answers["C1"].probability == 0.95


@pytest.mark.parametrize("status", [429, 500, 502, 503, 524, 529])
def test_transient_errors_are_retried_with_backoff(status):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(status, json={"error": {"code": status, "message": "busy"}})
        return httpx.Response(200, json=openrouter_reply({"C1": 0.7}))

    client, sleeps = make_client(handler)
    response = client.evaluate({}, build_questions([C1]))
    assert len(calls) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0]
    assert response.requests == 3


def test_retry_after_is_honoured_and_capped():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "120"}, json={})
        return httpx.Response(200, json=openrouter_reply({"C1": 0.7}))

    client, sleeps = make_client(handler)
    client.evaluate({}, build_questions([C1]))
    assert sleeps == [20.0]


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 413, 422])
def test_permanent_errors_are_not_retried(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"error": {"code": status, "message": "nope"}})

    client, sleeps = make_client(handler)
    with pytest.raises(JevError) as raised:
        client.evaluate({}, build_questions([C1]))
    assert len(calls) == 1 and sleeps == []
    assert raised.value.status == status and not raised.value.retriable
    assert "sk-test-secret" not in str(raised.value)


def test_retries_give_up_after_the_limit():
    def handler(request):
        return httpx.Response(529, json={})

    client, sleeps = make_client(handler, max_retries=2)
    with pytest.raises(JevError) as raised:
        client.evaluate({}, build_questions([C1]))
    assert raised.value.status == 529 and len(sleeps) == 2


def test_transport_failures_are_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=openrouter_reply({"C1": 0.7}))

    client, _ = make_client(handler)
    assert client.evaluate({}, build_questions([C1])).answers["C1"].probability == 0.7


def test_snapshot_outside_the_allowlist_is_drift():
    client, _ = make_client(lambda r: httpx.Response(200, json=openrouter_reply({"C1": 0.9},
                                                                                model="typesafe/jev-1.13-20261101")))
    with pytest.raises(JevModelDrift):
        client.evaluate({}, build_questions([C1]))


def test_another_model_family_is_drift_even_without_an_allowlist():
    client, _ = make_client(lambda r: httpx.Response(200, json=openrouter_reply({"C1": 0.9},
                                                                                model="typesafe/jev-1.14-20261201")),
                            allowed=())
    with pytest.raises(JevModelDrift):
        client.evaluate({}, build_questions([C1]))


@pytest.mark.parametrize("answers", [
    {}, {"C1": {"type": "choice", "choice": "x"}}, {"C1": {"type": "noul", "noul": 1.5}},
    {"C1": {"type": "noul", "noul": "0.5"}}, {"C1": {"type": "noul", "noul": True}},
])
def test_malformed_answers_are_rejected(answers):
    body = {"model": SNAPSHOT, "answers": answers, "usage": {}}
    client, _ = make_client(lambda r: httpx.Response(200, json=body))
    with pytest.raises(JevBadResponse):
        client.evaluate({}, build_questions([C1]))


def test_client_refuses_an_alias_and_a_missing_key():
    with pytest.raises(JevConfigError):
        LiveJevClient("openrouter", "k", "~typesafe/jev-latest")
    with pytest.raises(JevConfigError):
        LiveJevClient("openrouter", "", OR_MODEL)
    assert "sk-" not in repr(LiveJevClient("openrouter", "sk-secret", OR_MODEL))


# --- replay and cache ---------------------------------------------------------------------


def recorded(state, questions, probabilities, model=SNAPSHOT):
    response = jc.JevResponse({q: jc.JevAnswer(p, model, "live") for q, p in probabilities.items()},
                              {"input_tokens": 100}, 1)
    return {r.fingerprint: r for r in recordings_from_response(state, questions, response, OR_MODEL, "t#v1")}


def test_replay_returns_the_recorded_answer_and_misses_loudly():
    state = build_state(LINE, 48500, 0.1186)
    questions = build_questions([C1])
    replay = ReplayJevClient(recorded(state, questions, {"C1": 0.18}), OR_MODEL, (SNAPSHOT,))
    response = replay.evaluate(state, questions)
    assert response.answers["C1"] == jc.JevAnswer(0.18, SNAPSHOT, "replay")
    assert response.requests == 0
    other = build_state(LINE.model_copy(update={"commentary": "Something new."}), 48500, 0.1186)
    with pytest.raises(JevReplayMiss):
        replay.evaluate(other, questions)


def test_recordings_round_trip(tmp_path):
    state = build_state(LINE, 48500, 0.1186)
    records = recorded(state, build_questions([C1]), {"C1": 0.18})
    path = tmp_path / "rec.jsonl"
    write_recordings(path, records.values())
    assert load_recordings(path) == records


def test_cache_answers_repeats_without_a_live_call():
    state = build_state(LINE, 48500, 0.1186)
    questions = build_questions([C1])
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=openrouter_reply({"C1": 0.33}))

    live, _ = make_client(handler)
    caching = CachingJevClient(live, MemoryJevCache(), OR_MODEL, (SNAPSHOT,))
    first = caching.evaluate(state, questions)
    second = caching.evaluate(state, questions)
    assert len(calls) == 1
    assert first.answers["C1"].source == "live" and second.answers["C1"].source == "cache"
    assert second.answers["C1"].probability == 0.33 and second.requests == 0


def test_cache_ignores_answers_from_a_disallowed_snapshot():
    state = build_state(LINE, 48500, 0.1186)
    questions = build_questions([C1])
    stale = recorded(state, questions, {"C1": 0.9}, model="typesafe/jev-1.13-20250101")
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=openrouter_reply({"C1": 0.2}))

    live, _ = make_client(handler)
    caching = CachingJevClient(live, MemoryJevCache(stale), OR_MODEL, (SNAPSHOT,))
    assert caching.evaluate(state, questions).answers["C1"].probability == 0.2
    assert len(calls) == 1


# --- thresholds and calibration routing ------------------------------------------------------


@pytest.mark.parametrize("p,threshold,calibrated,expected", [
    (0.60, 0.60, True, "pass"),   # at the threshold passes
    (0.59, 0.60, True, "fail"),
    (0.59, 0.60, False, "prompt"),  # uncalibrated: routed to the reviewer, not failed
    (0.40, None, False, "prompt"),  # no threshold: default 0.5, and never calibrated
    (0.90, None, False, "pass"),
])
def test_grade(p, threshold, calibrated, expected):
    assert grade(p, threshold, calibrated).result == expected


def test_calibration_index_follows_the_fingerprint():
    fp = calibration_fingerprint(SNAPSHOT, C1.passes_when, C1.threshold)
    index = CalibrationIndex([("C1", fp, "calibrated")])
    assert index.is_calibrated(C1, SNAPSHOT)[0]
    feedback_edit = C1.model_copy(update={"feedback": "Who, specifically? Name them."})
    assert index.is_calibrated(feedback_edit, SNAPSHOT)[0]
    assert not index.is_calibrated(C1.model_copy(update={"passes_when": "Names the customer"}), SNAPSHOT)[0]
    assert not index.is_calibrated(C1.model_copy(update={"threshold": 0.7}), SNAPSHOT)[0]
    assert not index.is_calibrated(C1, "typesafe/jev-1.13-20261101")[0]
    assert not index.is_calibrated(C1.model_copy(update={"threshold": None}), SNAPSHOT)[0]
    later = CalibrationIndex([("C1", fp, "calibrated"), ("C1", fp, "uncalibrated")])
    assert not later.is_calibrated(C1, SNAPSHOT)[0]
