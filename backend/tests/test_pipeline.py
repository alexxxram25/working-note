"""Routing through the tiers, with an instrumented fake Jev that records every call."""

from __future__ import annotations

from typing import Dict, List

import pytest

from coach import pipeline
from coach.attempts import PriorAttempt
from coach.hashing import commentary_hash
from coach.jev.client import JevAnswer, JevError, JevResponse
from coach.jev.questions import QUESTION_BUILDER_VERSION, calibration_fingerprint
from coach.jev.thresholds import CalibrationIndex
from coach.models import Criterion, LineIn, Settings

SNAP = "typesafe/jev-1.13-20260917"


def criteria(**overrides) -> List[Criterion]:
    rows = [
        Criterion(id="C1", passes_when="Points to a specific vendor, customer or event",
                  feedback="Name the specific vendor, contract, hire, customer, or event that drove this movement.",
                  threshold=0.6),
        Criterion(id="C2", passes_when="Says what changed in the business", feedback="Tell me what changed.",
                  threshold=0.5),
        Criterion(id="RESTATE", passes_when="Adds something beyond the row", feedback="This repeats the row.",
                  threshold=0.5),
        Criterion(id="ARITH", feedback="The amounts you cite don't add up to the variance."),
        Criterion(id="DUP", feedback="This is word for word the same as another line."),
        Criterion(id="NEAR", feedback="This reads almost the same as another line."),
    ]
    return [c.model_copy(update=overrides.get(c.id, {})) for c in rows]


def calibrated(rows: List[Criterion]) -> CalibrationIndex:
    return CalibrationIndex([
        (c.id, calibration_fingerprint(SNAP, c.passes_when, c.threshold), "calibrated")
        for c in rows if c.kind == "jev"
    ])


class FakeJev:
    """Answers from a table keyed by commentary; records every call."""

    def __init__(self, table: Dict[str, Dict[str, float]], default: float = 0.9, fail_on: str = ""):
        self.table = table
        self.default = default
        self.fail_on = fail_on
        self.calls: List[Dict] = []

    def evaluate(self, state, questions, session_id=None):
        self.calls.append({"commentary": state["commentary"], "questions": sorted(questions)})
        if self.fail_on and self.fail_on in state["commentary"]:
            raise JevError("Jev returned 529: overloaded", status=529, retriable=True)
        scores = self.table.get(state["commentary"], {})
        return JevResponse({q: JevAnswer(scores.get(q, self.default), SNAP, "live") for q in questions},
                           {"input_tokens": 100}, 1)


def line(n: int, commentary: str, prior=100000, current=160000, period="2026-09") -> LineIn:
    return LineIn(line_id="FHD-US|{0}|MoM|{1}".format(period, 5000 + n), account="{0} Account".format(5000 + n),
                  basis="MoM", prior=prior, current=current, commentary=commentary)


def run(lines, jev=None, rows=None, index=None, prior=None, versions=None):
    rows = rows if rows is not None else criteria()
    config = pipeline.PipelineConfig(
        criteria=rows, settings=Settings(), calibration=index if index is not None else calibrated(rows),
        jev=jev, criteria_versions=versions or {})
    return pipeline.run(config, lines, prior)


def result_for(output, line_id):
    return next(r for r in output.results if r.line_id == line_id)


def test_immaterial_lines_never_reach_jev():
    jev = FakeJev({})
    out = run([line(1, "Flat.", prior=100000, current=101000)], jev)
    assert jev.calls == []
    r = out.results[0]
    assert r.status == "skipped_immaterial" and r.status_label == "Skipped: immaterial"
    assert [(row["criterion_id"], row["result"]) for row in out.log_rows] == [("MATERIALITY", "skipped")]


def test_arithmetic_failure_blocks_every_jev_question():
    jev = FakeJev({})
    bad = line(1, "Up $60K: vendor A ($30K) and vendor B ($20K).")
    out = run([bad], jev)
    assert jev.calls == []
    r = out.results[0]
    assert r.failed == ["ARITH"] and r.failed_cell == "ARITH"
    assert r.feedback_cell == "ARITH: The amounts you cite don't add up to the variance."
    blocked = {row["criterion_id"] for row in out.log_rows if row["result"] == "blocked"}
    assert blocked == {"C1", "C2", "RESTATE"}
    assert out.lines_sent_to_jev == []


def test_exact_duplicates_block_jev_but_near_duplicates_do_not():
    jev = FakeJev({})
    same = "Increase due to timing of the Stratus invoice."
    near_a = "Increase of $60K reflects new hires who started in September; see the headcount report."
    near_b = "Increase of $60K reflects new hires who started in September; see the headcount report HC."
    out = run([line(1, same), line(2, same), line(3, near_a), line(4, near_b)], jev)
    sent = {c["commentary"] for c in jev.calls}
    assert same not in sent
    assert {near_a, near_b} <= sent
    assert result_for(out, "FHD-US|2026-09|MoM|5001").failed == ["DUP"]
    near = result_for(out, "FHD-US|2026-09|MoM|5003")
    assert "NEAR" in near.failed and "NEAR (likely)" in near.failed_cell


def test_duplicates_are_only_compared_within_one_close():
    jev = FakeJev({})
    text = "Increase due to timing of the Stratus invoice."
    out = run([line(1, text, period="2026-08"), line(1, text, period="2026-09")], jev)
    assert all("DUP" not in r.failed for r in out.results)


def test_blank_commentary_needs_commentary_and_runs_nothing():
    jev = FakeJev({})
    out = run([line(1, "   ")], jev)
    r = out.results[0]
    assert r.status == "needs_commentary" and r.attempt_number == 0
    assert jev.calls == []
    assert [(row["criterion_id"], row["result"]) for row in out.log_rows] == [("COMMENTARY", "missing")]


def test_calibrated_failure_shows_the_reviewers_feedback_verbatim():
    text = "Hosting rose $60K because a large customer onboarded."
    out = run([line(1, text)], FakeJev({text: {"C1": 0.2}}))
    r = out.results[0]
    assert r.status == "needs_revision" and r.failed == ["C1"]
    assert r.failed_cell == "C1 (likely)"
    assert r.feedback_cell == "C1: Name the specific vendor, contract, hire, customer, or event that drove this movement."
    c1 = next(c for c in r.checks if c.criterion_id == "C1")
    assert (c1.probability, c1.threshold, c1.calibration_status, c1.model_version) == (0.2, 0.6, "Calibrated", SNAP)


def test_uncalibrated_criterion_routes_to_the_reviewer_not_the_preparer():
    text = "Hosting rose $60K because a large customer onboarded."
    rows = criteria()
    out = run([line(1, text)], FakeJev({text: {"C1": 0.2}}), rows=rows, index=CalibrationIndex())
    r = out.results[0]
    assert r.failed == [] and r.feedback_cell == ""
    assert r.prompts == ["C1"]
    assert r.status == "reviewer_prompt" and r.status_label == "Passed, reviewer prompt"
    assert any(row["result"] == "prompt" and row["calibration_status"] == "Uncalibrated" for row in out.log_rows)


def test_rewriting_feedback_keeps_the_verdict_and_shows_the_new_words():
    """Steps 1 to 3: the reviewer's new feedback appears; the pass condition is untouched."""
    text = "Hosting rose $60K because a large customer onboarded."
    before_rows = criteria()
    after_rows = criteria(C1={"feedback": "Who or what, specifically? Name the customer, vendor, contract or hire."})
    jev_before, jev_after = FakeJev({text: {"C1": 0.2}}), FakeJev({text: {"C1": 0.2}})
    before = run([line(1, text)], jev_before, rows=before_rows, index=calibrated(before_rows))
    after = run([line(1, text)], jev_after, rows=after_rows, index=calibrated(before_rows))
    assert jev_before.calls == jev_after.calls
    assert after.results[0].failed == before.results[0].failed == ["C1"]
    assert after.results[0].feedback_cell == (
        "C1: Who or what, specifically? Name the customer, vendor, contract or hire.")


def test_editing_the_pass_condition_makes_the_criterion_uncalibrated():
    text = "Hosting rose $60K because a large customer onboarded."
    original = criteria()
    edited = criteria(C1={"passes_when": "Names the customer by name"})
    out = run([line(1, text)], FakeJev({text: {"C1": 0.2}}), rows=edited, index=calibrated(original))
    assert out.results[0].prompts == ["C1"] and out.results[0].failed == []


def test_jev_errors_leave_the_line_ungraded_not_passed():
    text = "Hosting rose $60K because a large customer onboarded."
    out = run([line(1, text)], FakeJev({}, fail_on="Hosting"))
    r = out.results[0]
    assert r.status == "error" and r.status_label == "Not graded"
    assert {c.result for c in r.checks if c.tier == "jev"} == {"error"}


def test_no_jev_configured_is_an_error_not_a_pass():
    out = run([line(1, "Hosting rose $60K because Orion onboarded.")], jev=None)
    assert out.results[0].status == "error"


def test_inactive_criteria_are_neither_asked_nor_logged():
    jev = FakeJev({})
    rows = criteria(C2={"active": False}, NEAR={"active": False})
    out = run([line(1, "Hosting rose $60K because Orion Freight onboarded.")], jev, rows=rows)
    assert jev.calls[0]["questions"] == ["C1", "RESTATE"]
    logged = {row["criterion_id"] for row in out.log_rows}
    assert "C2" not in logged and "NEAR" not in logged


def test_every_criterion_inactive_is_refused_not_passed():
    rows = [c.model_copy(update={"active": False}) for c in criteria()]
    with pytest.raises(pipeline.NoActiveCriteria):
        run([line(1, "Hosting rose $60K because Orion Freight onboarded.")], FakeJev({}), rows=rows)


def test_a_criterion_without_a_pass_condition_is_an_error_for_that_criterion_only():
    jev = FakeJev({})
    rows = criteria(C2={"passes_when": " "})
    out = run([line(1, "Hosting rose $60K because Orion Freight onboarded.")], jev, rows=rows)
    checks = {c.criterion_id: c.result for c in out.results[0].checks}
    assert checks["C2"] == "error" and checks["C1"] == "pass"
    assert jev.calls[0]["questions"] == ["C1", "RESTATE"]


def test_malformed_and_repeated_line_ids_are_rejected():
    jev = FakeJev({})
    twice = line(1, "Hosting rose $60K because Orion Freight onboarded.")
    again = twice.model_copy(update={"commentary": "Hosting rose $60K because Orion Freight ran a data load.",
                                     "row": 9})
    once = line(2, "Payroll rose $60K because Orion Freight hired two engineers.")
    out = run([twice.model_copy(update={"row": 5}), again, once, LineIn(line_id="no-separators", commentary="x")],
              jev)
    # Both occurrences of the repeated ID are rejected: neither is graded, logged or sent to Jev.
    assert [r.line_id for r in out.results] == [once.line_id]
    assert [c["commentary"] for c in jev.calls] == [once.commentary]
    assert {r["line_id"] for r in out.log_rows} == {once.line_id}
    assert [(r["line_id"], r["row"], r["error"]) for r in out.rejected] == [
        (twice.line_id, "5", "Line ID appears 2 times in this run (rows 5, 9); none was graded"),
        (twice.line_id, "9", "Line ID appears 2 times in this run (rows 5, 9); none was graded"),
        ("no-separators", "", "Line ID needs entity|period|basis|account[|slice], got 1 part(s)"),
    ]


def test_attempt_numbers_follow_the_prior_attempt():
    text = "Hosting rose $60K because Orion Freight onboarded."
    lid = "FHD-US|2026-09|MoM|5001"
    prior = {lid: PriorAttempt(1, commentary_hash("old text"), failed=True)}
    out = run([line(1, text)], FakeJev({}), prior=prior)
    assert out.results[0].attempt_number == 2
    unchanged = {lid: PriorAttempt(1, commentary_hash(text), failed=True)}
    assert run([line(1, text)], FakeJev({}), prior=unchanged).results[0].attempt_number == 1


def test_log_rows_carry_the_spec_columns():
    out = run([line(1, "Hosting rose $60K because Orion Freight onboarded.")], FakeJev({}),
              versions={"C1": 3, "ARITH": 1})
    by_id = {row["criterion_id"]: row for row in out.log_rows}
    c1 = by_id["C1"]
    assert c1["close_period"] == "2026-09" and c1["attempt_number"] == 1
    assert c1["commentary_hash"].startswith("sha256:")
    assert c1["question_builder_version"] == QUESTION_BUILDER_VERSION
    assert c1["model_version"] == SNAP and c1["criterion_version"] == 3
    assert c1["calibration_status"] == "Calibrated" and c1["jev_source"] == "live"
    assert by_id["ARITH"]["question_builder_version"] is None
    assert by_id["ARITH"]["criterion_version"] == 1


def test_the_heartbeat_beats_once_per_jev_answer_and_can_stop_the_run():
    beats = []
    rows = criteria()
    lines = [line(i, "Hosting rose $60K because Orion Freight onboarded, line {0}.".format(i)) for i in range(3)]
    config = pipeline.PipelineConfig(criteria=rows, settings=Settings(), calibration=calibrated(rows),
                                     jev=FakeJev({}), heartbeat=lambda: beats.append(1))
    pipeline.run(config, lines)
    assert len(beats) == 3

    class Lost(RuntimeError):
        pass

    def lose():
        raise Lost()

    config = pipeline.PipelineConfig(criteria=rows, settings=Settings(), calibration=calibrated(rows),
                                     jev=FakeJev({}), heartbeat=lose)
    with pytest.raises(Lost):
        pipeline.run(config, lines)


def test_one_request_per_line_with_every_jev_question():
    jev = FakeJev({})
    lines = [line(i, "Line {0}: hosting rose $60K because customer {0} onboarded.".format(i)) for i in range(1, 6)]
    out = run(lines, jev)
    assert len(jev.calls) == 5 and out.jev_requests == 5
    assert all(call["questions"] == ["C1", "C2", "RESTATE"] for call in jev.calls)
