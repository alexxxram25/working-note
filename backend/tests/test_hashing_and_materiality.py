from __future__ import annotations

import pytest

from coach.checks.materiality import Variance, assess, variance_of
from coach.hashing import commentary_hash, content_hash, normalize_commentary

# --- commentary hash -----------------------------------------------------------


def test_layout_changes_are_not_new_attempts():
    base = commentary_hash("Up $48K because Orion Freight onboarded.")
    assert commentary_hash("  Up $48K because   Orion Freight onboarded.  ") == base
    assert commentary_hash("Up $48K because\nOrion Freight onboarded.") == base
    assert commentary_hash("Up $48K because Orion Freight onboarded.") == base


def test_unicode_composition_does_not_change_the_hash():
    composed = "Café renewal"
    decomposed = "Café renewal"
    assert commentary_hash(composed) == commentary_hash(decomposed)


def test_any_change_to_the_words_is_a_new_hash():
    assert commentary_hash("Up $48K.") != commentary_hash("Up $49K.")
    assert commentary_hash("Up $48K.") != commentary_hash("up $48K.")


def test_blank_and_none_hash_the_same():
    assert normalize_commentary(None) == normalize_commentary("   ") == ""
    assert commentary_hash(None) == commentary_hash("")
    assert commentary_hash("x").startswith("sha256:")


def test_content_hash_ignores_key_order():
    assert content_hash({"a": 1, "b": [1, 2]}) == content_hash({"b": [1, 2], "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


# --- materiality gate ----------------------------------------------------------

USD, PCT = 25000, 0.10


@pytest.mark.parametrize("usd,pct,material", [
    (24999, 0.0999, False),   # under both: skipped
    (25000, 0.05, True),      # equal to the $ threshold is material
    (1000, 0.10, True),       # equal to the % threshold is material
    (24999, 0.11, True),      # over % only
    (30000, 0.01, True),      # over $ only
    (-24999, -0.0999, False), # direction does not matter
    (-30000, -0.01, True),
    (0, 0.0, False),
])
def test_gate_skips_only_when_under_both_thresholds(usd, pct, material):
    assert assess(Variance(usd, pct), USD, PCT).material is material


def test_prior_of_zero_lets_dollars_decide():
    small = variance_of(prior=0, current=500)
    assert small.pct is None
    assert assess(small, USD, PCT).material is False
    large = variance_of(prior=0, current=118000)
    assert assess(large, USD, PCT).material is True
    assert "undefined" in assess(large, USD, PCT).reason


def test_float_noise_on_the_threshold_still_counts_as_equal():
    # 0.3 - 0.2 is 0.09999999999999998 in binary floating point.
    assert assess(Variance(1000, 0.3 - 0.2), USD, PCT).material is True


def test_variance_is_derived_only_when_blank():
    assert variance_of(100, 150) == Variance(50.0, 0.5)
    assert variance_of(100, 150, variance_usd=60).usd == 60.0
    assert variance_of(-200, -100).pct == pytest.approx(0.5)
    with pytest.raises(ValueError):
        variance_of(None, 100)
