from __future__ import annotations

import pytest

from coach import feedback
from coach.attempts import PriorAttempt, is_second_attempt, next_attempt
from coach.checks.duplicates import find, normalize_near, similarity
from coach.models import CheckOutcome, Criterion, LineIn, Settings, parse_number

# --- duplicates ----------------------------------------------------------------


def test_exact_duplicates_ignore_case_and_whitespace():
    result = find([("A", "Increase due to timing."), ("B", "increase  due to TIMING."), ("C", "Other.")], 0.85)
    assert result["A"].exact_with == ("B",)
    assert result["B"].exact_with == ("A",)
    assert not result["C"].exact


def test_near_duplicates_mask_amounts():
    a = "Increase of $42K reflects new hires who started in August; see the August headcount report."
    b = "Increase of $30K reflects new hires who started in August; see the August headcount report."
    result = find([("A", a), ("B", b)], 0.85)
    assert not result["A"].exact
    assert result["A"].near_with[0][0] == "B"
    assert result["A"].near_with[0][1] >= 0.99
    assert normalize_near(a) == normalize_near(b)


def test_threshold_is_respected():
    a = "Two engineers joined the London team on Jul 1, adding $28K a month. Recurring."
    b = "Two engineers joined the Applications team on Jul 6, adding $34K a month. Recurring."
    score = similarity(normalize_near(a), normalize_near(b))
    assert find([("A", a), ("B", b)], score + 0.01)["A"].near is False
    assert find([("A", a), ("B", b)], score - 0.01)["A"].near is True


def test_blank_commentary_is_never_a_duplicate():
    result = find([("A", ""), ("B", "   "), ("C", "Text.")], 0.85)
    assert set(result) == {"C"}


def test_exact_duplicates_are_not_also_reported_as_near():
    result = find([("A", "Same text."), ("B", "Same text.")], 0.5)
    assert result["A"].exact and not result["A"].near


# --- attempts --------------------------------------------------------------------

FAILED = PriorAttempt(1, "sha256:a", failed=True)
PASSED = PriorAttempt(2, "sha256:a", failed=False)


@pytest.mark.parametrize("prior,new_hash,blank,expected", [
    (None, "sha256:a", False, 1),                 # first look
    (FAILED, "sha256:a", False, 1),               # rerun, unchanged: not an attempt
    (FAILED, "sha256:b", False, 2),               # changed after failing: second attempt
    (PASSED, "sha256:b", False, 2),               # changed after passing: polishing, not a retry
    (None, "sha256:e", True, 0),                  # blank: nothing submitted
    (FAILED, "sha256:e", True, 1),                # blanked out later: number does not move
    (PriorAttempt(0, "sha256:e", False), "sha256:c", False, 1),  # first text after a blank
])
def test_attempt_rules(prior, new_hash, blank, expected):
    assert next_attempt(prior, new_hash, blank) == expected


def test_second_attempt_flag():
    assert is_second_attempt(FAILED, 2)
    assert not is_second_attempt(FAILED, 1)
    assert not is_second_attempt(None, 1)


# --- feedback ----------------------------------------------------------------------

CRITERIA = [
    Criterion(id="C1", feedback="Name the specific vendor, contract, hire, customer, or event that drove this movement."),
    Criterion(id="C4", feedback="Point me to something I can pull: the invoice, JE, contract, or headcount report."),
    Criterion(id="C5", feedback=""),
    Criterion(id="ARITH", feedback="The amounts you cite don't add up to the variance."),
]


def outcome(cid, result, tier="jev", likely=True, **kw):
    return CheckOutcome(criterion_id=cid, tier=tier, result=result, likely=likely, **kw)


def test_feedback_is_the_reviewers_text_verbatim_in_criteria_order():
    outcomes = [outcome("C4", "fail"), outcome("C1", "fail"), outcome("ARITH", "pass", tier="deterministic",
                                                                      likely=False)]
    failed, failed_cell, items, cell, prompts = feedback.assemble(outcomes, CRITERIA)
    assert failed == ["C1", "C4"]
    assert failed_cell == "C1 (likely), C4 (likely)"
    assert cell == "C1: " + CRITERIA[0].feedback + "\nC4: " + CRITERIA[1].feedback
    assert [i.text for i in items] == [CRITERIA[0].feedback, CRITERIA[1].feedback]
    assert prompts == []


def test_deterministic_failures_are_not_labeled_likely():
    _, failed_cell, _, _, _ = feedback.assemble([outcome("ARITH", "fail", tier="deterministic", likely=False)],
                                                CRITERIA)
    assert failed_cell == "ARITH"


def test_blank_feedback_is_never_filled_in():
    failed, failed_cell, items, cell, _ = feedback.assemble([outcome("C5", "fail")], CRITERIA)
    assert failed == ["C5"] and failed_cell == "C5 (likely)"
    assert items[0].text == ""
    assert cell == ""


def test_prompts_are_routed_not_failed():
    failed, _, _, cell, prompts = feedback.assemble([outcome("C1", "prompt")], CRITERIA)
    assert failed == [] and cell == "" and prompts == ["C1"]
    assert feedback.status_of([outcome("C1", "prompt")], material=True, blank=False) == feedback.REVIEWER_PROMPT


@pytest.mark.parametrize("outcomes,material,blank,expected", [
    ([], False, False, feedback.SKIPPED),
    ([], True, True, feedback.NEEDS_COMMENTARY),
    ([outcome("C1", "fail"), outcome("C2", "error")], True, False, feedback.NEEDS_REVISION),
    ([outcome("C1", "error")], True, False, feedback.ERROR),
    ([outcome("C1", "pass"), outcome("ARITH", "abstain", tier="deterministic")], True, False, feedback.PASSED),
])
def test_status(outcomes, material, blank, expected):
    assert feedback.status_of(outcomes, material, blank) == expected


def test_note_carries_probabilities_and_facts_not_advice():
    text = feedback.note([
        outcome("C1", "fail", probability=0.18, threshold=0.58, model_version="jev-1.13.0"),
        outcome("ARITH", "pass", tier="deterministic", likely=False, detail="Drivers $52,000 + $13,000 = $65,000"),
    ], CRITERIA, attempt=2)
    assert "C1: p=0.18 < threshold 0.58, likely not met" in text
    assert "ARITH: pass (Drivers $52,000 + $13,000 = $65,000)" in text
    assert "Model jev-1.13.0" in text and "Attempt 2" in text


# --- models from spreadsheet values -------------------------------------------------


def test_criterion_reads_sheet_values():
    c = Criterion.from_sheet_row({"ID": " C1 ", "Threshold": "0.62", "Active (Y/N)": "y", "Last reviewed": "2026-09-08T00:00:00"})
    assert (c.id, c.threshold, c.active, c.last_reviewed) == ("C1", 0.62, True, "2026-09-08")
    assert Criterion.from_sheet_row({"ID": "ARITH", "Threshold": "n/a"}).threshold is None
    assert Criterion.from_sheet_row({"ID": "NEAR", "Threshold": "Settings"}).kind == "heuristic"
    with pytest.raises(ValueError):
        Criterion.from_sheet_row({"ID": "C1", "Threshold": "1.5"})
    with pytest.raises(ValueError):
        Criterion.from_sheet_row({"ID": "C1", "Active (Y/N)": "maybe"})


def test_criterion_content_excludes_status_fields():
    a = Criterion(id="C1", feedback="x", last_reviewed="2026-01-01", calibration_status="Calibrated")
    b = Criterion(id="C1", feedback="x", last_reviewed="2026-09-08", calibration_status="Uncalibrated")
    assert a.sheet_content() == b.sheet_content()


def test_settings_read_percentages_however_typed():
    rows = [
        {"Setting": "Materiality ($)", "Value": "$25,000"},
        {"Setting": "Materiality (%)", "Value": "10%"},
        {"Setting": "Near-duplicate similarity", "Value": 0.9},
        {"Setting": "Health: always-fails rate", "Value": 60},
        {"Setting": "Pinned model version", "Value": " jev-1.13.0 "},
        {"Setting": "Something a reviewer added", "Value": "ignored"},
    ]
    s = Settings.from_sheet_rows(rows)
    assert (s.materiality_usd, s.materiality_pct, s.near_duplicate_threshold, s.health_always_fails_rate) == (
        25000, 0.10, 0.9, 0.60)
    assert s.pinned_model_version == "jev-1.13.0"
    with pytest.raises(ValueError):
        Settings.from_sheet_rows([{"Setting": "Minimum sample size", "Value": "2.5"}])


def test_line_numbers_parse_accounting_formats():
    line = LineIn(line_id="x", prior="(1,200)", current="$3,400", variance_pct="", commentary=None)
    assert (line.prior, line.current, line.variance_pct, line.commentary) == (-1200.0, 3400.0, None, "")
    assert LineIn(line_id="x", variance_pct="(4.5%)").variance_pct == pytest.approx(-0.045)


@pytest.mark.parametrize("value,expected", [(0.912, 0.912), ("91.2%", 0.912), ("0.6", 0.6), (0.85, 0.85), (0.999, 0.999)])
def test_thresholds_with_up_to_three_decimals_are_read(value, expected):
    assert Criterion(id="C1", threshold=value).threshold == pytest.approx(expected)


@pytest.mark.parametrize("value", [0.9125, "0.9125", "91.25%", 0.60001, 1 / 3])
def test_thresholds_with_more_than_three_decimals_are_refused(value):
    # The calibration fingerprint and the Criteria snapshot both carry the threshold
    # to three decimals, so a finer value would be graded on one number and
    # looked up (and recorded) under another.
    with pytest.raises(ValueError, match="at most three decimals"):
        Criterion(id="C1", threshold=value)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "nan", "inf", "Infinity", "-Infinity",
                                 "1e999", "#DIV/0!", "30,00O", 10 ** 400, -(10 ** 400)])
def test_nonfinite_and_junk_numbers_are_refused_everywhere(bad):
    with pytest.raises(ValueError):
        parse_number(bad)
    with pytest.raises(ValueError):
        LineIn(line_id="x", prior=bad)
    with pytest.raises(ValueError):
        Criterion(id="C1", threshold=bad)
    for setting in ("Minimum sample size", "Materiality ($)", "Materiality (%)", "Health: reviewer override weight"):
        with pytest.raises(ValueError):  # a count of infinity used to be an OverflowError, a 500
            Settings.from_sheet_rows([{"Setting": setting, "Value": bad}])
    with pytest.raises(ValueError):
        Settings(materiality_usd=float("inf"))
