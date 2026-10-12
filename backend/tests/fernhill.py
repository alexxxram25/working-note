"""Fernhill Data, the fictional company the tests run on.

Three monthly closes (July to September 2026) with every draft and revision of
every line, ground-truth labels per criterion, the reviewer's overrides, and
672 real Jev answers recorded once from the pinned snapshot and replayed
strictly. The files in fixtures/fernhill/ are frozen: they are test data, never
shipped, never seeded into a deployment, and never copied into the image.

seed() gives a test's store what a team would have after setting up and
calibrating: two people on the team list and Fernhill's calibrations.
simulate_close() works a close the way a team would: each round submits every
line's current commentary, and a line that comes back failing (or blank)
moves to its next authored version, if there is one.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

from coach.labeled_set import REQUIRED_COLUMNS, LabeledText
from coach.models import LineIn, LineResult

DIR = Path(__file__).resolve().parent / "fixtures" / "fernhill"
RECORDINGS = DIR / "jev_recordings.jsonl"
SNAPSHOT = "typesafe/jev-1.13-20260917"
CLOSES = ["2026-07", "2026-08", "2026-09"]
CLOSE = "2026-09"
HOSTING_LINE = "FHD-US|2026-09|MoM|5000"  # Cloud hosting, up $48.5K: the line the five steps follow
JEV_CRITERIA = ["C1", "C2", "C3", "C4", "C5", "RESTATE"]
RETRY_STATUSES = ("needs_revision", "needs_commentary")


def _read(name: str) -> List[Dict[str, str]]:
    with open(DIR / name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def flux_rows(period: str) -> List[Dict[str, str]]:
    return _read("flux_{0}.csv".format(period))


def criteria_rows() -> List[Dict[str, str]]:
    """Fernhill's Criteria tab: the starter wording, with the thresholds its calibration chose."""
    return _read("criteria.csv")


def settings_rows() -> List[Dict[str, str]]:
    return _read("settings.csv")


def labels() -> List[Dict[str, str]]:
    return _read("labels.csv")


def overrides(period: Optional[str] = None) -> List[Dict[str, str]]:
    return [r for r in _read("overrides.csv") if period is None or r["close_period"] == period]


def versions(period: Optional[str] = None) -> Dict[str, List[str]]:
    """Line ID -> commentary versions in order (v1 is the first draft)."""
    out: Dict[str, List[str]] = {}
    for row in sorted(_read("commentary_versions.csv"), key=lambda r: (r["line_id"], int(r["version"]))):
        if period is None or row["close_period"] == period:
            out.setdefault(row["line_id"], []).append(row["commentary"])
    return out


def line_in(row: Dict[str, str], commentary: Optional[str] = None) -> LineIn:
    return LineIn(
        line_id=row["Line ID"], entity=row["Entity"], period=row["Period"], basis=row["Comparison basis"],
        account=row["Account"], prior=row["Prior"], current=row["Current"],
        variance_usd=row["Variance ($)"], variance_pct=row["Variance (%)"] or None,
        commentary=row["Commentary"] if commentary is None else commentary,
    )


def lines_for(period: str, texts: Optional[Dict[str, str]] = None) -> List[LineIn]:
    texts = texts or {}
    return [line_in(row, texts.get(row["Line ID"])) for row in flux_rows(period)]


def labeled() -> List[LabeledText]:
    """Every labeled Fernhill text, in the shape calibration reads."""
    rows = {r["Line ID"]: r for close in CLOSES for r in flux_rows(close)}
    return [LabeledText(label["text_id"], line_in(rows[label["line_id"]], label["commentary"]),
                        {cid: label[cid] for cid in JEV_CRITERIA})
            for label in labels()]


def write_team_labels(path: Path) -> Path:
    """Fernhill's labels as a team's onboarding labels.csv, for scripts/calibrate.py --labels."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(REQUIRED_COLUMNS + JEV_CRITERIA + ["reason"])
        rows = {r["Line ID"]: r for close in CLOSES for r in flux_rows(close)}
        for label in labels():
            row = rows[label["line_id"]]
            writer.writerow([label["text_id"], row["Account"], row["Comparison basis"], row["Prior"], row["Current"],
                             label["commentary"]] + [label[c] for c in JEV_CRITERIA] + [label["note"]])
    return path


def seed_team(store) -> None:
    """reviewer@example.com and preparer@example.com, as scripts/team.py would add them."""
    for member in _read("team_roles.csv"):
        store.upsert_team_member(member["email"], member["role"], member.get("display_name"))


def seed_calibrations(store) -> None:
    """Fernhill's calibrations, as scripts/calibrate.py --write-db would record them."""
    report = json.loads((DIR / "calibration_report.json").read_text())
    for entry in report["criteria"]:
        store.add_calibration(dict(entry, labeled_set_hash=report["labeled_set_hash"],
                                   label_source=report.get("label_source", "synthetic")))


def seed(store) -> None:
    """The team list and the calibrations a team would have after setting up and onboarding."""
    seed_team(store)
    seed_calibrations(store)


def services(**overrides):
    """A memory-store service on the recorded answers, seeded with Fernhill's team and calibrations."""
    from coach.api import build_services
    from coach.config import Config

    settings = dict(env="development", store="memory", auth_modes=("local",), jev_mode="replay",
                    jev_recordings=RECORDINGS, jev_allowed_snapshots=(SNAPSHOT,))
    settings.update(overrides)
    built = build_services(Config(**settings))
    seed(built.store)
    return built


class Round(NamedTuple):
    number: int
    texts: Dict[str, str]
    results: List[LineResult]


Runner = Callable[[List[LineIn]], List[LineResult]]


def simulate_close(period: str, runner: Runner, max_rounds: int = 5,
                   hold: Sequence[str] = (), start: Optional[Dict[str, int]] = None) -> List[Round]:
    """Run a close to rest. `hold` lists Line IDs that keep their current version."""
    all_versions = versions(period)
    position = dict(start or {})
    for line_id in all_versions:
        position.setdefault(line_id, 0)
    rounds: List[Round] = []
    for number in range(1, max_rounds + 1):
        texts = {lid: all_versions[lid][position[lid]] for lid in all_versions}
        results = runner(lines_for(period, texts))
        rounds.append(Round(number, texts, results))
        moved = False
        for result in results:
            lid = result.line_id
            if lid in hold or result.status not in RETRY_STATUSES:
                continue
            if position[lid] + 1 < len(all_versions[lid]):
                position[lid] += 1
                moved = True
        if not moved:
            break
    return rounds
