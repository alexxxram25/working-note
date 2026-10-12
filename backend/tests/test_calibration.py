from __future__ import annotations

import pytest

from coach.calibration import Example, Floors, calibrate, choose_threshold, evaluate, split


def examples(passing, failing):
    out = [Example("p{0}".format(i), p, True) for i, p in enumerate(passing)]
    out += [Example("f{0}".format(i), p, False) for i, p in enumerate(failing)]
    return out


def test_split_is_stratified_disjoint_and_deterministic():
    data = examples([0.9] * 40, [0.1] * 12)
    train, test = split(data, "seed")
    assert {e.text_id for e in train}.isdisjoint({e.text_id for e in test})
    assert len(train) + len(test) == len(data)
    assert sum(1 for e in test if e.passes) == 10 and sum(1 for e in test if not e.passes) == 3
    assert split(data, "seed") == (train, test)
    assert split(list(reversed(data)), "seed")[1] == test  # independent of input order
    assert split(data, "other")[1] != test


def test_small_classes_keep_at_least_one_on_each_side():
    train, test = split(examples([0.9] * 10, [0.1, 0.2]), "s")
    assert sum(1 for e in test if not e.passes) == 1
    assert sum(1 for e in train if not e.passes) == 1


def test_separable_data_gets_a_threshold_between_the_groups():
    data = examples([0.7, 0.8, 0.9, 0.95], [0.05, 0.1, 0.3])
    threshold = choose_threshold(data)
    assert 0.3 < threshold <= 0.7
    assert evaluate(data, threshold).balanced_accuracy == 1.0


def test_balanced_accuracy_does_not_reward_always_passing():
    data = examples([0.9] * 18, [0.85, 0.2])
    always_pass = evaluate(data, 0.01)
    assert always_pass.accuracy == pytest.approx(0.9)
    assert always_pass.balanced_accuracy == pytest.approx(0.5)


def test_metrics():
    m = evaluate(examples([0.9, 0.4], [0.2, 0.7]), 0.5)
    assert (m.n, m.n_pass, m.n_fail) == (4, 2, 2)
    assert m.balanced_accuracy == pytest.approx(0.5)
    assert m.fail_recall == pytest.approx(0.5) and m.fail_precision == pytest.approx(0.5)
    assert m.brier == pytest.approx(((0.9 - 1) ** 2 + (0.4 - 1) ** 2 + 0.2 ** 2 + 0.7 ** 2) / 4)


def test_calibrated_when_held_out_slice_agrees():
    passing = [0.6 + 0.01 * i for i in range(30)]
    failing = [0.05 + 0.01 * i for i in range(12)]
    result = calibrate("C1", examples(passing, failing), seed="s")
    assert result.status == "Calibrated", result.reason
    assert result.holdout.n_fail >= 2 and result.holdout.balanced_accuracy == 1.0


def test_uncalibrated_when_the_model_does_not_separate_the_labels():
    noisy = examples([0.2, 0.8, 0.3, 0.7, 0.4, 0.6] * 4, [0.75, 0.25, 0.65, 0.35] * 3)
    result = calibrate("C3", noisy, seed="s", floors=Floors(min_balanced_accuracy=0.9))
    assert result.status == "Uncalibrated"
    assert "balanced accuracy" in result.reason


def test_uncalibrated_when_too_few_labels_are_held_out():
    result = calibrate("C9", examples([0.9] * 20, [0.1]), seed="s")
    assert result.status == "Uncalibrated"
    assert "needs 2 of each" in result.reason


def test_a_fixed_threshold_is_checked_not_chosen():
    data = examples([0.6 + 0.01 * i for i in range(30)], [0.05 + 0.01 * i for i in range(12)])
    result = calibrate("C1", data, seed="s", fixed_threshold=0.42)
    assert result.threshold == 0.42
