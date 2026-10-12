"""The Google Sheet's shape, in one place.

Column headers are the contract between the sheet, the service and the
onboarding files. The Apps Script carries its own copy (it cannot import Python),
and a test holds the two to the same strings, so a renamed header fails a test
instead of silently writing results into the wrong column.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional

FLUX = "Flux"
CRITERIA = "Criteria"
SETTINGS = "Settings"
LOG = "Log"
CRITERIA_HISTORY = "Criteria History"
CRITERIA_HEALTH = "Criteria Health"
SECOND_ATTEMPTS = "Second Attempts"

TABS = [FLUX, CRITERIA, SETTINGS, LOG, CRITERIA_HISTORY, CRITERIA_HEALTH, SECOND_ATTEMPTS]

FLUX_COLUMNS = [
    "Line ID",
    "Entity",
    "Period",
    "Comparison basis",
    "Account",
    "Prior",
    "Current",
    "Variance ($)",
    "Variance (%)",
    "Commentary",
    "Status",
    "Failed criteria",
    "Feedback",
    "Override",
    "Override note",
]

CRITERIA_COLUMNS = [
    "ID",
    "Criterion",
    "Passes when",
    "Feedback when failed",
    "Why it matters",
    "Example of good",
    "Owner",
    "Last reviewed",
    "Threshold",
    "Calibration status",
    "Active (Y/N)",
]

SETTINGS_COLUMNS = ["Setting", "Value", "Notes"]

LOG_COLUMNS = [
    "Timestamp",
    "Close period",
    "Line ID",
    "Run ID",
    "Attempt number",
    "Commentary hash",
    "Criterion ID",
    "Result",
    "Jev probability",
    "Criterion version",
    "Question-builder version",
    "Model version",
    "Calibration status",
]

CRITERIA_HISTORY_COLUMNS = [
    "Criterion ID",
    "Version",
    "Changed at",
    "Changed by",
    "Fields changed",
    "Before",
    "After",
    "Calibration impact",
]

CRITERIA_HEALTH_COLUMNS = [
    "Criterion ID",
    "Criterion",
    "Signal",
    "What it suggests",
    "Evidence",
    "Close window",
    "Resolution",
    "Resolved by",
    "Resolved at",
    # The service's ID for the prompt, so a Resolution can be sent back. The
    # script hides this column; nobody needs to read it.
    "Prompt ID",
]

# The Second Attempts tab adds one column per close period after these two.
SECOND_ATTEMPTS_LEAD_COLUMNS = ["Criterion ID", "Criterion"]

HEALTH_RESOLUTIONS = ["edit", "retire", "keep"]

# Override dropdown values are built from the active criteria; these are its
# two halves. "not covered" is the gap bucket.
OVERRIDE_FLAG_WRONG = "Flag was wrong"
OVERRIDE_MISSED = "Missed an issue"
OVERRIDE_NOT_COVERED = "not covered"


class SettingSpec(NamedTuple):
    label: str
    key: str
    default: object
    kind: str  # usd | fraction | probability | count | weight | text
    notes: str


# The settings a reviewer reads first (Materiality $, Materiality %, Pinned
# model version, health signal thresholds, minimum sample size), then the
# check thresholds that live in Settings rather than in a column of their own.
SETTINGS_ROWS: List[SettingSpec] = [
    SettingSpec("Materiality ($)", "materiality_usd", 25000, "usd",
                "A line is skipped when its $ variance is under this AND its % variance is under Materiality (%). Equal counts as material."),
    SettingSpec("Materiality (%)", "materiality_pct", 0.10, "fraction",
                "When Prior is zero the % variance is undefined and the $ test decides alone."),
    SettingSpec("Pinned model version", "pinned_model_version", "jev-1.13.0", "text",
                "Must match the service's pin. A mismatch is reported as an error; it never switches the model."),
    SettingSpec("Health: never-fails rate", "health_never_fails_rate", 0.05, "fraction",
                "Prompt when a criterion's first-attempt fail rate is at or below this."),
    SettingSpec("Health: always-fails rate", "health_always_fails_rate", 0.60, "fraction",
                "Prompt when a criterion's first-attempt fail rate is at or above this."),
    SettingSpec("Health: near-threshold band", "health_near_band", 0.05, "probability",
                "Jev probabilities within this distance of the criterion's threshold count as near it. Jev's "
                "call-to-call spread is about 0.01, so 0.05 is close enough that a rerun could flip the verdict."),
    SettingSpec("Health: near-threshold share", "health_near_share", 0.35, "fraction",
                "Prompt when at least this share of a criterion's probabilities sit near its threshold."),
    SettingSpec("Health: flag-wrong weighted count", "health_flag_wrong_min", 3, "count",
                "Prompt when weighted 'Flag was wrong' overrides on a criterion reach this."),
    SettingSpec("Health: missed-issue weighted count", "health_missed_min", 3, "count",
                "Prompt when weighted 'Missed an issue' overrides on a criterion (or 'not covered') reach this."),
    SettingSpec("Health: reviewer override weight", "health_reviewer_weight", 2, "weight",
                "Reviewer overrides count for more than preparer overrides."),
    SettingSpec("Health: preparer override weight", "health_preparer_weight", 1, "weight",
                "A preparer saying a flag is wrong starts a conversation; it still counts."),
    SettingSpec("Health: stale after (closes)", "health_stale_closes", 2, "count",
                "Prompt when a criterion has not been reviewed in this many closes."),
    SettingSpec("Health: window (closes)", "health_window_closes", 1, "count",
                "How many recent closes the Log-based signals read."),
    SettingSpec("Minimum sample size", "min_sample_size", 20, "count",
                "No prompt fires until a criterion has at least this many material lines behind it."),
    SettingSpec("Near-duplicate similarity", "near_duplicate_threshold", 0.85, "probability",
                "Commentary at or above this similarity to another line is flagged as a likely near-duplicate."),
    SettingSpec("Arithmetic tolerance ($)", "arith_tolerance_usd", 1000, "usd",
                "Cited amounts may miss the variance by the larger of this and Arithmetic tolerance (%), plus the rounding the citation implies."),
    SettingSpec("Arithmetic tolerance (%)", "arith_tolerance_pct", 0.01, "fraction",
                "Share of the variance allowed as tolerance."),
]


def setting_by_label(label: str) -> Optional[SettingSpec]:
    for spec in SETTINGS_ROWS:
        if spec.label == label:
            return spec
    return None


def setting_by_key(key: str) -> Optional[SettingSpec]:
    for spec in SETTINGS_ROWS:
        if spec.key == key:
            return spec
    return None
