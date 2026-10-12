"""Fernhill's deterministic expectations, checked by the real engine.

The labels for arithmetic, exact duplicates and near duplicates were written
by hand before the engine existed. Here the engine has to agree with every one
of them, and no pair of texts in a close may look like a duplicate unless the
dataset says it was designed to.
"""

from __future__ import annotations

import csv
import itertools
from collections import defaultdict

import pytest

from coach.checks import arithmetic, duplicates
from coach.checks.duplicates import normalize_exact, normalize_near, similarity
from coach.models import Settings

SETTINGS = Settings()
CLOSES = ["2026-07", "2026-08", "2026-09"]


def _rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def flux(fixtures_dir):
    return {r["Line ID"]: r for c in CLOSES for r in _rows(fixtures_dir / "flux_{0}.csv".format(c))}


@pytest.fixture(scope="module")
def labels(fixtures_dir):
    return _rows(fixtures_dir / "labels.csv")


@pytest.fixture(scope="module")
def versions(fixtures_dir):
    return _rows(fixtures_dir / "commentary_versions.csv")


def test_every_arithmetic_expectation_holds(flux, labels):
    mismatches = []
    for label in labels:
        row = flux[label["line_id"]]
        pct = float(row["Variance (%)"]) if row["Variance (%)"] else None
        result = arithmetic.check(
            label["commentary"], float(row["Variance ($)"]), pct, float(row["Prior"]), float(row["Current"]),
            SETTINGS.arith_tolerance_usd, SETTINGS.arith_tolerance_pct,
        )
        if result.result != label["arith_expected"]:
            mismatches.append((label["text_id"], label["arith_expected"], result.result, result.detail))
    assert not mismatches, mismatches
    assert len(labels) >= 100


def test_designed_duplicates_are_found_in_first_drafts(flux, labels):
    for close in CLOSES:
        drafts = [(l["line_id"], l["commentary"]) for l in labels if l["close_period"] == close and l["version"] == "1"]
        found = duplicates.find(drafts, SETTINGS.near_duplicate_threshold)
        expected = {l["line_id"]: l for l in labels if l["close_period"] == close and l["version"] == "1"}
        for line_id, result in found.items():
            assert ("fail" if result.exact else "pass") == expected[line_id]["dup_expected"], line_id
            assert ("fail" if result.near else "pass") == expected[line_id]["near_expected"], line_id


def test_no_accidental_duplicates_between_any_versions(versions, labels):
    """Whatever order revisions land in, only the designed pairs ever collide."""
    designed = {(l["line_id"], l["version"]) for l in labels
                if l["dup_expected"] == "fail" or l["near_expected"] == "fail"}
    texts = defaultdict(list)
    for v in versions:
        if v["material"] == "Y" and v["commentary"].strip():
            texts[v["close_period"]].append((v["line_id"], v["version"], v["commentary"]))
    collisions = []
    for close, items in texts.items():
        for (la, va, ta), (lb, vb, tb) in itertools.combinations(items, 2):
            if la == lb:
                continue
            exact = normalize_exact(ta) == normalize_exact(tb)
            near = similarity(normalize_near(ta), normalize_near(tb)) >= SETTINGS.near_duplicate_threshold
            if (exact or near) and not ((la, va) in designed and (lb, vb) in designed):
                collisions.append((close, la, va, lb, vb))
    assert not collisions, collisions


def test_closest_undesigned_pair_has_headroom(versions, labels):
    """Report how close the nearest accidental pair gets, and require some room."""
    designed = {l["line_id"] for l in labels if l["near_expected"] == "fail" or l["dup_expected"] == "fail"}
    worst = 0.0
    for close in CLOSES:
        items = [(v["line_id"], v["commentary"]) for v in versions
                 if v["close_period"] == close and v["material"] == "Y" and v["commentary"].strip()]
        for (la, ta), (lb, tb) in itertools.combinations(items, 2):
            if la == lb or (la in designed and lb in designed):
                continue
            worst = max(worst, similarity(normalize_near(ta), normalize_near(tb)))
    assert worst < SETTINGS.near_duplicate_threshold - 0.05, worst
