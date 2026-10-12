"""The starter Criteria and Settings a new deployment begins with.

starter_criteria.csv is the one source. PostgresStore gets it through
db/seed.sql, which seed_sql() writes (scripts/write_seed.py); MemoryStore gets
it from seed_defaults(). Settings come from sheet_schema's defaults.

Nothing else is seeded: no team (the first reviewer is added with
scripts/team.py) and no calibrations. Every Jev-graded starter criterion is
Uncalibrated until the team calibrates it on its own labeled lines, so until
then its flags go to the reviewer as prompts, never to the preparer as
failures. The starter thresholds are starting values, not measurements.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from . import sheet_schema as schema
from .hashing import canonical_json, content_hash
from .models import Settings
from .store.memory import MemoryStore

STARTER_CRITERIA = Path(__file__).with_name("starter_criteria.csv")


def starter_criteria() -> List[Dict[str, str]]:
    with open(STARTER_CRITERIA, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def default_settings() -> Dict[str, Any]:
    return {spec.key: spec.default for spec in schema.SETTINGS_ROWS}


def seed_defaults(store: MemoryStore) -> None:
    store.set_defaults(default_settings(), starter_criteria())


def settings_rows(values: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Settings tab rows from a key -> value mapping, in the sheet's order."""
    merged = dict(default_settings())
    merged.update({k: v for k, v in values.items() if v is not None})
    Settings(**merged)  # validate
    return [{"Setting": spec.label, "Value": merged[spec.key], "Notes": spec.notes} for spec in schema.SETTINGS_ROWS]


def _sql(value: object) -> str:
    """A SQL literal for this module's own content (never user input)."""
    return "'" + str(value).replace("'", "''") + "'"


def seed_sql() -> str:
    """db/seed.sql: the starter Settings and Criteria under sheet_id '*'. Idempotent."""
    specs = schema.SETTINGS_ROWS
    snapshot = starter_criteria()
    lines = [
        "-- Written by scripts/write_seed.py from backend/coach/starter_criteria.csv and the",
        "-- Settings defaults in backend/coach/sheet_schema.py. Do not edit by hand.",
        "-- Idempotent: safe to apply more than once.",
        "--",
        "-- No team and no calibrations: add the first reviewer with scripts/team.py, and",
        "-- calibrate on the team's own labeled lines with scripts/calibrate.py.",
        "",
        "-- Deployment defaults (sheet_id '*').",
        "insert into settings (sheet_id, key, value) values",
    ]
    lines += [
        "    ('*', {0}, {1}::jsonb){2}".format(_sql(spec.key), _sql(json.dumps(spec.default)),
                                             "," if i < len(specs) - 1 else "")
        for i, spec in enumerate(specs)
    ]
    lines += [
        "on conflict (sheet_id, key) do nothing;",
        "",
        "-- The starter Criteria tab: C1 to C5, RESTATE and the three rules in code, all uncalibrated.",
        "insert into criteria_snapshots (sheet_id, content_hash, criteria, captured_by) values",
        "    ('*', {0}, {1}::jsonb, 'seed')".format(_sql(content_hash(snapshot)), _sql(canonical_json(snapshot))),
        "on conflict (sheet_id, content_hash) do nothing;",
    ]
    return "\n".join(lines) + "\n"
