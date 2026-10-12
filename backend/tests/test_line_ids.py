from __future__ import annotations

import json

import pytest

from coach.line_ids import LineIdError, account_key, build_line_id, parse_line_id, period_of
from conftest import ROOT

VECTORS = json.loads((ROOT / "apps_script" / "test" / "fixtures" / "line_id_vectors.json").read_text())


@pytest.mark.parametrize("case", VECTORS["build"], ids=lambda c: c["expected"])
def test_shared_vectors_build(case):
    assert build_line_id(case["entity"], case["period"], case["basis"], case["account"], case["slice"]) == case["expected"]


@pytest.mark.parametrize("case", VECTORS["invalid_build"], ids=lambda c: repr(c["entity"]) + repr(c["account"]))
def test_shared_vectors_reject_bad_parts(case):
    with pytest.raises(LineIdError):
        build_line_id(case["entity"], case["period"], case["basis"], case["account"], case["slice"])


@pytest.mark.parametrize("bad", VECTORS["invalid_parse"])
def test_shared_vectors_reject_malformed_ids(bad):
    with pytest.raises(LineIdError):
        parse_line_id(bad)


def test_parse_round_trips_with_and_without_slice():
    plain = parse_line_id("FHD-US|2026-09|MoM|5000")
    assert plain.entity == "FHD-US" and plain.period == "2026-09" and plain.slice is None
    sliced = parse_line_id("FHD-US|2026-09|MoM|6000|PLAT")
    assert sliced.account_key == "6000" and sliced.slice == "PLAT"
    assert period_of("FHD-US|2026-09|MoM|6000|PLAT") == "2026-09"


def test_account_is_display_only_once_the_id_exists():
    """Renaming the account text changes nothing as long as the number leads."""
    assert account_key("5000 Cloud hosting") == account_key("5000 Hosting (Stratus)") == "5000"


def test_new_close_period_is_a_new_line_id():
    july = build_line_id("FHD-US", "2026-07", "MoM", "5000 Cloud hosting")
    august = build_line_id("FHD-US", "2026-08", "MoM", "5000 Cloud hosting")
    assert july != august
