#!/usr/bin/env python3
"""Choose and check each Jev criterion's threshold on a team's labeled sample.

    # The sample from onboarding, after record_jev.py --labels has recorded Jev's answers:
    python scripts/calibrate.py --labels labels.csv --criteria criteria.csv \
        --recordings team_recordings.jsonl --out team_calibration.json --write-db

For every labeled text, looks up the recorded Jev probability for each active
Jev criterion, splits the set 75/25 stratified by label with a fixed seed,
picks the threshold that maximizes balanced accuracy on the 75%, and checks
it on the held-out 25%. A criterion whose held-out check misses the floors is
reported Uncalibrated; the service routes its flags to the reviewer as
prompts.

--write-db records the results in the deployment's calibrations table
(DATABASE_URL), which is what the service reads to decide whether a criterion
is calibrated. Copy each threshold into the Criteria tab's Threshold column:
the fingerprint includes it, so a sheet threshold that differs from the
calibrated one is, correctly, uncalibrated.

A recording missing for any labeled question stops the run: calibrating on a
subset of the labels would quietly change what the thresholds mean.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from coach import envfile, labeled_set  # noqa: E402
from coach.calibration import Example, Floors, calibrate  # noqa: E402
from coach.checks.materiality import variance_of  # noqa: E402
from coach.hashing import content_hash  # noqa: E402
from coach.jev.questions import (QUESTION_BUILDER_VERSION, build_question, build_state,  # noqa: E402
                                 calibration_fingerprint, passes_when_hash, question_fingerprint)
from coach.jev.replay import load_recordings  # noqa: E402
from coach.models import Criterion  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--labels", type=Path, required=True, help="the team's labeled CSV from onboarding")
    parser.add_argument("--criteria", type=Path, required=True, help="the team's criteria.csv")
    parser.add_argument("--recordings", type=Path, required=True, help="Jev answers from record_jev.py --labels")
    parser.add_argument("--out", type=Path, required=True, help="where to write the report (.json, plus .md)")
    # The split's fixed seed. Its value is arbitrary; changing it changes every split.
    parser.add_argument("--seed", default="flux-coach-2026")
    parser.add_argument("--holdout", type=float, default=0.25)
    parser.add_argument("--min-balanced-accuracy", type=float, default=0.75)
    parser.add_argument("--min-per-class", type=int, default=2)
    parser.add_argument("--write-db", action="store_true", help="record calibrations in DATABASE_URL")
    args = parser.parse_args()

    recordings = load_recordings(args.recordings)
    if not recordings:
        print("No recordings in {0}. Run scripts/record_jev.py first.".format(args.recordings), file=sys.stderr)
        return 1
    request_models = sorted({r.request_model for r in recordings.values()})
    if len(request_models) != 1:
        print("Recordings mix request models {0}; calibrate one at a time.".format(request_models), file=sys.stderr)
        return 1
    request_model = request_models[0]

    with open(args.criteria, newline="", encoding="utf-8") as handle:
        criteria = [c for c in (Criterion.from_sheet_row(r) for r in csv.DictReader(handle))
                    if c.kind == "jev" and c.active]
    items = labeled_set.load(args.labels, [c.id for c in criteria])

    examples: Dict[str, List[Example]] = {c.id: [] for c in criteria}
    snapshots = set()
    missing = []
    for item in items:
        variance = variance_of(item.line.prior, item.line.current, item.line.variance_usd, item.line.variance_pct)
        state = build_state(item.line, variance.usd, variance.pct)
        for criterion in criteria:
            record = recordings.get(question_fingerprint(request_model, state, build_question(criterion)))
            if record is None:
                missing.append("{0} {1}".format(item.text_id, criterion.id))
                continue
            snapshots.add(record.model)
            examples[criterion.id].append(Example(item.text_id, record.probability,
                                                  item.labels[criterion.id] == "pass"))
    if missing:
        print("{0} labeled question(s) have no recording, e.g. {1}. Run scripts/record_jev.py --yes."
              .format(len(missing), missing[:3]), file=sys.stderr)
        return 1
    if len(snapshots) != 1:
        print("Recordings come from several snapshots {0}; record again on one.".format(sorted(snapshots)),
              file=sys.stderr)
        return 1
    snapshot = snapshots.pop()

    labeled_set_hash = content_hash(sorted([i.text_id] + [i.labels[c.id] for c in criteria] for i in items))
    floors = Floors(args.min_balanced_accuracy, args.min_per_class)
    report = {
        "label_source": "team",
        "labeled_set_hash": labeled_set_hash,
        "labeled_texts": len(items),
        "seed": args.seed,
        "holdout_fraction": args.holdout,
        "floors": floors._asdict(),
        "request_model": request_model,
        "model_snapshot": snapshot,
        "question_builder_version": QUESTION_BUILDER_VERSION,
        "criteria": [],
    }
    for criterion in criteria:
        result = calibrate(criterion.id, examples[criterion.id], args.seed, floors, args.holdout)
        report["criteria"].append({
            "criterion_id": criterion.id,
            "threshold": result.threshold,
            "status": result.status,
            "reason": result.reason,
            "fingerprint": calibration_fingerprint(snapshot, criterion.passes_when, result.threshold),
            "passes_when_hash": passes_when_hash(criterion.passes_when),
            "model_snapshot": snapshot,
            "question_builder_version": QUESTION_BUILDER_VERSION,
            "metrics": {"calibration": result.calibration.as_dict(), "holdout": result.holdout.as_dict()},
        })

    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Calibration report",
        "",
        "Team labels from onboarding.",
        "",
        "- Model snapshot: `{0}` (requested as `{1}`)".format(snapshot, request_model),
        "- Question builder: `{0}`".format(QUESTION_BUILDER_VERSION),
        "- Labeled texts: {0}; held out: {1:.0%}, stratified by label, seed `{2}`".format(
            len(items), args.holdout, args.seed),
        "- Floors: held-out balanced accuracy >= {0:.2f}, >= {1} pass and fail labels held out".format(
            floors.min_balanced_accuracy, floors.min_holdout_per_class),
        "",
        "| Criterion | Threshold | Status | Held-out balanced accuracy | Held-out n (pass/fail) | "
        "Fail recall | Fail precision | Brier | Why |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in report["criteria"]:
        held = entry["metrics"]["holdout"]
        lines.append("| {0} | {1:.2f} | {2} | {3:.2f} | {4} ({5}/{6}) | {7} | {8} | {9:.3f} | {10} |".format(
            entry["criterion_id"], entry["threshold"], entry["status"], held["balanced_accuracy"], held["n"],
            held["n_pass"], held["n_fail"],
            "n/a" if held["fail_recall"] is None else "{0:.2f}".format(held["fail_recall"]),
            "n/a" if held["fail_precision"] is None else "{0:.2f}".format(held["fail_precision"]),
            held["brier"], entry["reason"]))
    args.out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[8:]))

    if args.write_db:
        envfile.load()
        dsn = os.environ.get("DATABASE_URL", "").strip()
        if not dsn:
            print("--write-db needs DATABASE_URL", file=sys.stderr)
            return 1
        from coach.store.postgres import PostgresStore

        store = PostgresStore(dsn)
        for entry in report["criteria"]:
            store.add_calibration(dict(entry, labeled_set_hash=labeled_set_hash, label_source=report["label_source"],
                                       created_by="scripts/calibrate.py"))
        print("RAN: {0} calibration(s) recorded in the database".format(len(report["criteria"])))
    print("RAN: calibration written to {0}".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
