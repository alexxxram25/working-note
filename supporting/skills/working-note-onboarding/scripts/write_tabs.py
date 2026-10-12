#!/usr/bin/env python3
"""Check an onboarding package and write the Criteria, Settings and calibration files.

    python write_tabs.py package.json --out onboarding/
    python write_tabs.py package.json --out onboarding/ --calibration onboarding/team_calibration.json
    python write_tabs.py --check onboarding/

The package is what the interview settled on (SKILL.md describes it;
assets/package_example.json is a complete one). Written, in the sheet's exact
column order:

    criteria.csv            the Criteria tab
    settings.csv            the Settings tab
    labels.csv              the labeled sample: the team's calibration set
    onboarding_summary.md   what was decided, what was rejected and why, what to run next

Nothing is written if the package has an error. --check validates the same
three files when they came from the chat prompt (supporting/onboarding_prompt.md)
rather than from a package.

The checks back up the interview; they do not replace it. They refuse what
the Coach cannot use: a pass condition that asks whether an explanation is
true (the Coach never judges business truth), one that is a score rather than
a yes or no, feedback with a placeholder in it (preparers see it word for
word), labels on criteria that code checks, a default criterion nobody
decided about.

Standard library only, so the Skill runs wherever Python 3.8 or later does.
The column orders and rules are copies of the service's; a test in the repo
(backend/tests/test_onboarding.py) holds them equal.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

ASSETS = Path(__file__).resolve().parents[1] / "assets"

CRITERIA_COLUMNS = [
    "ID", "Criterion", "Passes when", "Feedback when failed", "Why it matters", "Example of good",
    "Owner", "Last reviewed", "Threshold", "Calibration status", "Active (Y/N)",
]
SETTINGS_COLUMNS = ["Setting", "Value", "Notes"]
LABEL_COLUMNS = ["text_id", "account", "comparison_basis", "prior", "current", "commentary"]
REASON_COLUMN = "reason"

# Checked by code in the service, never by Jev: not labeled, not calibrated.
SYSTEM_IDS = ("ARITH", "DUP", "NEAR")
# IDs the service uses for its own outcomes in the Log.
RESERVED_IDS = ("MATERIALITY", "COMMENTARY")
CALIBRATION_STATUSES = ("Calibrated", "Uncalibrated", "Deterministic", "Heuristic")
KNOWN_BASES = ("MoM", "QoQ", "YoY", "vs Budget", "BvA", "vs Forecast")
MAX_COMMENTARY = 20000

MATERIALITY_USD = "Materiality ($)"
MATERIALITY_PCT = "Materiality (%)"
PINNED = "Pinned model version"
SETTING_KINDS = {
    MATERIALITY_USD: "usd",
    MATERIALITY_PCT: "fraction",
    PINNED: "text",
    "Health: never-fails rate": "fraction",
    "Health: always-fails rate": "fraction",
    "Health: near-threshold band": "probability",
    "Health: near-threshold share": "fraction",
    "Health: flag-wrong weighted count": "count",
    "Health: missed-issue weighted count": "count",
    "Health: reviewer override weight": "weight",
    "Health: preparer override weight": "weight",
    "Health: stale after (closes)": "count",
    "Health: window (closes)": "count",
    "Minimum sample size": "count",
    "Near-duplicate similarity": "probability",
    "Arithmetic tolerance ($)": "usd",
    "Arithmetic tolerance (%)": "fraction",
}
SETTING_ALIASES = {"materiality_usd": MATERIALITY_USD, "materiality_pct": MATERIALITY_PCT}

# calibrate.py holds out a quarter of each label, and calls a criterion
# Calibrated only with at least two passes and two fails held out.
HOLDOUT = 0.25
MIN_PER_CLASS = 2

FIELDS = {
    "criterion": "Criterion", "passes_when": "Passes when", "feedback": "Feedback when failed",
    "why": "Why it matters", "example": "Example of good", "owner": "Owner",
}
SYSTEM_EDITABLE = ("criterion", "feedback", "why", "example", "owner")
CRITERION_KEYS = set(FIELDS) | {"id", "from_default", "active", "question"}
SAMPLE_KEYS = set(LABEL_COLUMNS) | {"fails", "reason", "source"}
PACKAGE_KEYS = {"team", "owner", "reviewed_on", "criteria", "rejected", "settings", "sample", "notes"}

ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
TRUTH_WORDS = re.compile(
    r"\b(?:correct(?:ly)?|incorrect(?:ly)?|accurate(?:ly)?|inaccurate|true(?!-)|truthful|plausibl[ey]|"
    r"reasonabl[ey]|realistic|valid|invalid|justified|makes sense|right(?!-))\b", re.IGNORECASE)
SCALE_WORDS = re.compile(r"\b(?:scores?|scored|scoring|ratings?|on a scale|out of (?:\d+|five|ten))\b",
                         re.IGNORECASE)
TASTE_WORDS = re.compile(
    r"\b(?:clear(?:ly)?|concise(?:ly)?|well[- ]written|high[- ]quality|good|professional|sufficient(?:ly)?|"
    r"adequate(?:ly)?|appropriate(?:ly)?|thorough(?:ly)?|detailed|enough)\b", re.IGNORECASE)
YES_NO_START = re.compile(r"^(?:Does|Do|Is|Are|Has|Have|Can|Will|Did|Was|Were)\b", re.IGNORECASE)
PLACEHOLDER = re.compile(r"[{}<>\[\]]|\b(?:TODO|TBD|XXX|lorem)\b", re.IGNORECASE)


class Problems:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append("{0}: {1}".format(where, message))

    def warn(self, where: str, message: str) -> None:
        self.warnings.append("{0}: {1}".format(where, message))


class Readiness(NamedTuple):
    criterion_id: str
    fails: int
    passes: int
    ready: bool


# ---------------------------------------------------------------------------
# Small rules shared with the service
# ---------------------------------------------------------------------------


def normalize(text: Any) -> str:
    """NFC with whitespace collapsed: how the service compares pass conditions."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(text))).strip()


def passes_when_hash(text: Any) -> str:
    return "sha256:" + hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def parse_number(value: Any) -> Optional[float]:
    """Spreadsheet numbers: '' is missing, '(1,200)' is negative, '10%' is 0.1."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in ("n/a", "na", "-", "none"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[,$\s]", "", text.strip("()"))
    percent = text.endswith("%")
    number = float(text.rstrip("%"))  # ValueError for words
    if percent:
        number /= 100.0
    return -number if negative else number


def number_text(value: float) -> str:
    text = format(value, ".15g")
    return "0" if text == "-0" else text


def held_out(count: int) -> int:
    """How many of `count` same-label texts calibrate.py holds out (coach.calibration.split)."""
    cut = int(round(count * HOLDOUT))
    if count >= 2:
        cut = max(1, min(cut, count - 1))
    return cut


LABELS_NEEDED = next(n for n in range(1000) if held_out(n) >= MIN_PER_CLASS)


def is_immaterial(prior: float, current: float, usd: float, pct: float) -> bool:
    """The service's gate: under the $ threshold AND under the % one (prior zero: $ alone).

    Rounded the way the service rounds (cents, nine places of a fraction), so a
    line exactly at a threshold counts as material here too.
    """
    variance = current - prior
    if round(abs(variance), 2) >= round(usd, 2):
        return False
    if prior == 0:
        return True
    return round(abs(variance / abs(prior)), 9) < round(pct, 9)


# ---------------------------------------------------------------------------
# Lints for the wording of a criterion
# ---------------------------------------------------------------------------


# Room for one condition and what doesn't count. Spelling out the near misses ("Fails when the
# cause is a category, even with an amount attached") is what made C1 reliable; see
# docs/evidence/c1-blind-check.
MAX_PASS_CONDITION = 400


def lint_pass_condition(where: str, text: str, problems: Problems) -> None:
    if not text:
        problems.error(where, "Passes when is empty; it is what Jev's yes-or-no question is built from")
        return
    if len(text) > MAX_PASS_CONDITION:
        problems.error(where, "Passes when runs to {0} characters; keep it to one condition and what doesn't "
                              "count".format(len(text)))
    if text.endswith("?"):
        problems.error(where, "Passes when is a condition the commentary meets (\"Names the customer\"), "
                              "not a question")
    truth = TRUTH_WORDS.search(text)
    if truth:
        problems.error(where, "'{0}' asks whether the explanation is true. The Coach checks whether "
                              "commentary can be reviewed, never whether it is right; name something a "
                              "reader can see in the text instead".format(truth.group(0)))
    scale = SCALE_WORDS.search(text)
    if scale:
        problems.error(where, "'{0}' makes this a score, not a yes or no".format(scale.group(0)))
    taste = TASTE_WORDS.search(text)
    if taste:
        problems.warn(where, "'{0}' is a judgment two reviewers could make differently. A sharper "
                             "condition names what a reader would see, like \"names the customer\""
                             .format(taste.group(0)))


def lint_question(where: str, question: Any, problems: Problems) -> None:
    text = normalize(question)
    if not text:
        problems.error(where, "no yes-or-no question. Write the question this criterion asks of a line "
                              "(\"Does the commentary name the customer?\"); a criterion that can't be "
                              "asked as one is rejected")
    elif not (YES_NO_START.match(text) and text.endswith("?") and text.count("?") == 1):
        problems.error(where, "\"{0}\" is not a yes-or-no question. It should read like \"Does the "
                              "commentary ...?\", with one question mark".format(text))


def lint_feedback(where: str, text: str, problems: Problems) -> None:
    if not text:
        problems.error(where, "Feedback when failed is empty; it is the only thing a preparer reads "
                              "when this criterion fails")
        return
    placeholder = PLACEHOLDER.search(text)
    if placeholder:
        problems.error(where, "Feedback when failed has a placeholder ('{0}'); the Coach shows this text "
                              "word for word".format(placeholder.group(0)))
    if len(text) > 400:
        problems.warn(where, "Feedback when failed runs to {0} characters; preparers read it on every "
                             "failed line".format(len(text)))


def lint_jev_row(where: str, row: Dict[str, str], problems: Problems, shipped_wording: bool = False) -> None:
    if not row["Criterion"]:
        problems.error(where, "Criterion (the short name) is empty")
    # A pass condition kept word for word from the defaults was measured with its threshold; the
    # wording lints are for conditions a team writes or edits.
    if not shipped_wording:
        lint_pass_condition(where, row["Passes when"], problems)
    lint_feedback(where, row["Feedback when failed"], problems)


def check_across(rows: Sequence[Dict[str, str]], problems: Problems) -> None:
    active = [r for r in rows if r["Active (Y/N)"] == "Y" and r["ID"] not in SYSTEM_IDS]
    for column in ("Passes when", "Feedback when failed"):
        seen: Dict[str, str] = {}
        for row in active:
            key = normalize(row[column]).lower()
            if key and key in seen:
                problems.warn("criterion " + row["ID"], "{0} is the same as {1}'s".format(column, seen[key]))
            seen.setdefault(key, row["ID"])
    if not active:
        problems.warn("criteria", "no active criterion for Jev to grade; only the arithmetic and "
                                  "duplicate checks will run")
    elif len(active) > 8:
        problems.warn("criteria", "{0} active criteria for Jev. Each is another question on every line and "
                                  "another message a preparer may read; fewer, sharper ones coach better"
                                  .format(len(active)))


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]
        return list(reader.fieldnames or []), rows


def load_defaults() -> Tuple[Dict[str, Dict[str, str]], List[Dict[str, str]]]:
    _, criteria = read_csv(ASSETS / "default_criteria.csv")
    _, settings = read_csv(ASSETS / "default_settings.csv")
    return {row["ID"]: row for row in criteria}, settings


# ---------------------------------------------------------------------------
# Package -> rows
# ---------------------------------------------------------------------------


def _flag(value: Any, where: str, problems: Problems) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("y", "yes", "true", "1"):
        return True
    if text in ("n", "no", "false", "0", "retired"):
        return False
    problems.error(where, "active must be true or false, got {0!r}".format(value))
    return True


def build_criteria(package: Dict[str, Any], defaults: Dict[str, Dict[str, str]], reviewed_on: str,
                   problems: Problems) -> Tuple[List[Dict[str, str]], Dict[str, str]]:
    """Criteria tab rows, and a one-line account of each row for the summary."""
    entries = package.get("criteria")
    if not isinstance(entries, list) or not entries:
        problems.error("criteria", "list the criteria the interview settled on")
        return [], {}
    team_owner = normalize(package.get("owner"))
    seen = set()
    jev_active: List[Dict[str, str]] = []
    retired: List[Dict[str, str]] = []
    system: Dict[str, Dict[str, str]] = {}
    origins: Dict[str, str] = {}
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            problems.error("criteria[{0}]".format(index), "each criterion is an object with an id")
            continue
        cid = str(entry.get("id") or "").strip()
        where = "criterion " + (cid or "#{0}".format(index))
        if not ID_PATTERN.match(cid):
            problems.error(where, "an ID starts with a letter and uses letters, digits, - and _ (24 at most)")
            continue
        if cid.upper() in RESERVED_IDS:
            problems.error(where, "{0} is the service's name for its own results; pick another ID".format(cid))
            continue
        if cid in seen:
            problems.error(where, "appears more than once")
            continue
        seen.add(cid)
        unknown = sorted(set(entry) - CRITERION_KEYS)
        if unknown:
            problems.error(where, "unknown field(s) {0}; criteria take {1}".format(
                ", ".join(unknown), ", ".join(sorted(CRITERION_KEYS))))
        active = _flag(entry.get("active", True), where, problems)
        base = defaults.get(cid)

        if cid in SYSTEM_IDS:
            row = dict(base)
            if "passes_when" in entry and normalize(entry["passes_when"]) != normalize(base["Passes when"]):
                problems.error(where, "{0} is a rule in code; its Passes when describes the code and is not "
                                      "edited here".format(cid))
            for field in SYSTEM_EDITABLE:
                if field in entry:
                    row[FIELDS[field]] = normalize(entry[field])
            row["Last reviewed"] = reviewed_on
            row["Active (Y/N)"] = "Y" if active else "N"
            if active:
                lint_feedback(where, row["Feedback when failed"], problems)
            else:
                problems.warn(where, "retired: the {0} check will not run".format(row["Criterion"].lower()))
            system[cid] = row
            origins[cid] = "system rule" + ("" if active else ", retired")
            continue

        if entry.get("from_default"):
            if base is None:
                problems.error(where, "from_default, but {0} is not a default criterion (defaults: {1})".format(
                    cid, ", ".join(c for c in defaults if c not in SYSTEM_IDS)))
                continue
            row = dict(base)
        else:
            row = {column: "" for column in CRITERIA_COLUMNS}
            row["ID"] = cid
        for field in FIELDS:
            if field in entry:
                row[FIELDS[field]] = normalize(entry[field])
        # A kept default keeps its owner, so keeping it word for word makes no new version.
        row["Owner"] = row["Owner"] or team_owner or "Reviewer"
        row["Last reviewed"] = reviewed_on
        row["Active (Y/N)"] = "Y" if active else "N"

        default_wording = base is not None and normalize(base["Passes when"]) == row["Passes when"]
        if default_wording:
            # Same pass condition, same threshold: the shipped calibration still describes it.
            row["Threshold"], row["Calibration status"] = base["Threshold"], base["Calibration status"]
        else:
            row["Threshold"], row["Calibration status"] = "", "Uncalibrated"

        if base is None:
            origin = "new"
        else:
            edited = [c for c in ("Criterion", "Passes when", "Feedback when failed", "Why it matters",
                                  "Example of good", "Owner") if normalize(base[c]) != row[c]]
            origin = "default" if not edited else "default, edited: " + ", ".join(edited)
        if active:
            lint_jev_row(where, row, problems, shipped_wording=default_wording)
            if not default_wording:
                lint_question(where, entry.get("question"), problems)
            jev_active.append(row)
            origins[cid] = origin
        else:
            retired.append(row)
            origins[cid] = origin + ", retired"

    for cid, base in defaults.items():
        if cid in SYSTEM_IDS and cid not in system:
            row = dict(base)
            row["Last reviewed"] = reviewed_on
            system[cid] = row
            origins[cid] = "system rule"
        elif cid not in SYSTEM_IDS and cid not in seen:
            problems.error("criterion " + cid, "is in the default set but not in the package. Decide it with "
                                               "the reviewer: keep it ({{\"id\": \"{0}\", \"from_default\": true}}), "
                                               "edit it, or retire it (add \"active\": false)".format(cid))

    ordered_system = [system[c] for c in SYSTEM_IDS if c in system]
    rows = (jev_active + [r for r in ordered_system if r["Active (Y/N)"] == "Y"] + retired
            + [r for r in ordered_system if r["Active (Y/N)"] != "Y"])
    check_across(rows, problems)
    return rows, origins


def setting_value(label: str, value: Any, problems: Problems) -> Optional[str]:
    where = "setting " + label
    kind = SETTING_KINDS[label]
    if kind == "text":
        text = normalize(value)
        if label == PINNED and not re.match(r"^jev-\d+\.\d+\.\d+$", text):
            problems.error(where, "must look like jev-1.13.0 and match the service's JEV_PINNED_VERSION")
            return None
        return text
    try:
        number = parse_number(value)
    except ValueError:
        problems.error(where, "{0!r} is not a number".format(value))
        return None
    if number is None:
        problems.error(where, "needs a value")
        return None
    if kind in ("fraction", "probability"):
        if number > 1.0:
            number /= 100.0  # 10 means 10%, as it does in the sheet
        if not 0.0 <= number <= 1.0:
            problems.error(where, "must be between 0 and 1 (0% to 100%)")
            return None
        if label == MATERIALITY_PCT and number == 0:
            problems.warn(where, "0% makes every line with any change material")
        return number_text(number)
    if number < 0:
        problems.error(where, "must not be negative")
        return None
    if kind == "count" and number != int(number):
        problems.error(where, "must be a whole number")
        return None
    return number_text(number)


def build_settings(package: Dict[str, Any], defaults: List[Dict[str, str]],
                   problems: Problems) -> Tuple[List[Dict[str, str]], Dict[str, Tuple[str, str]]]:
    given = package.get("settings")
    if not isinstance(given, dict):
        problems.error("settings", "record the materiality the reviewer confirmed (step 5), even if it stays "
                                   "at $25,000 and 10%")
        given = {}
    values: Dict[str, Any] = {}
    for key, value in given.items():
        label = SETTING_ALIASES.get(key, key)
        if label not in SETTING_KINDS:
            problems.error("settings", "unknown setting {0!r}. The service reads only these labels, and "
                                       "ignores any other row: {1}".format(key, ", ".join(SETTING_KINDS)))
            continue
        values[label] = value
    for label in (MATERIALITY_USD, MATERIALITY_PCT):
        if label not in values:
            problems.error("setting " + label, "not confirmed with the reviewer (step 5)")
    rows: List[Dict[str, str]] = []
    changed: Dict[str, Tuple[str, str]] = {}
    for default in defaults:
        label = default["Setting"]
        text = default["Value"]
        if label in values:
            checked = setting_value(label, values[label], problems)
            text = default["Value"] if checked is None else checked
        if text != default["Value"]:
            changed[label] = (default["Value"], text)
            if label == PINNED:
                problems.warn("setting " + label, "the service refuses a run whose pin differs from its own "
                                                  "JEV_PINNED_VERSION")
        rows.append({"Setting": label, "Value": text, "Notes": default["Notes"]})
    return rows, changed


def build_sample(package: Dict[str, Any], criteria_rows: Sequence[Dict[str, str]],
                 settings_rows: Sequence[Dict[str, str]], problems: Problems) -> List[Dict[str, str]]:
    items = package.get("sample") or []
    if not isinstance(items, list):
        problems.error("sample", "the labeled sample is a list of lines")
        return []
    jev_ids = [r["ID"] for r in criteria_rows if r["ID"] not in SYSTEM_IDS and r["Active (Y/N)"] == "Y"]
    retired = {r["ID"] for r in criteria_rows if r["Active (Y/N)"] != "Y"}
    rows: List[Dict[str, str]] = []
    seen = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            problems.error("sample[{0}]".format(index), "each labeled line is an object")
            continue
        text_id = normalize(item.get("text_id")) or "S{0:02d}".format(index)
        where = "sample " + text_id
        if text_id in seen:
            problems.error(where, "text_id appears more than once")
            continue
        seen.add(text_id)
        unknown = sorted(set(item) - SAMPLE_KEYS)
        if unknown:
            problems.error(where, "unknown field(s) {0}".format(", ".join(unknown)))
        account = normalize(item.get("account"))
        if not account:
            problems.error(where, "account is empty")
        basis = normalize(item.get("comparison_basis")) or "MoM"
        if "|" in basis:
            problems.error(where, "comparison_basis can't contain '|'")
        elif basis.lower() not in [b.lower() for b in KNOWN_BASES]:
            problems.warn(where, "comparison basis {0!r} is passed to Jev as written; the usual ones are {1}"
                          .format(basis, ", ".join(KNOWN_BASES)))
        figures = {}
        for name in ("prior", "current"):
            try:
                figures[name] = parse_number(item.get(name))
            except ValueError:
                figures[name] = None
            if figures[name] is None:
                problems.error(where, "{0} must be an amount; Jev reads the row's figures with the "
                                      "commentary".format(name))
        commentary = str(item.get("commentary") or "").strip()
        if not commentary:
            problems.error(where, "commentary is empty. A blank line is caught before Jev, so it teaches "
                                  "nothing about the standard")
        elif len(commentary) > MAX_COMMENTARY:
            problems.error(where, "commentary is longer than the service accepts")
        fails = item.get("fails") or []
        if isinstance(fails, str):
            fails = [f for f in re.split(r"[,\s]+", fails) if f]
        fails = [str(f).strip() for f in fails]
        for failed in fails:
            if failed in SYSTEM_IDS:
                problems.error(where, "{0} is checked by code, not by Jev, so it is never labeled. Say it "
                                      "in the reason instead".format(failed))
            elif failed in retired:
                problems.error(where, "{0} is retired; label only active criteria".format(failed))
            elif failed not in jev_ids:
                problems.error(where, "marks {0}, which is not a criterion in the package".format(failed))
        reason = normalize(item.get("reason"))
        if fails and not reason:
            problems.error(where, "marks a failure without a reason. The reviewer's one sentence on why is "
                                  "what criteria are drafted from")
        row = {"text_id": text_id, "account": account, "comparison_basis": basis,
               "prior": number_text(figures["prior"]) if figures["prior"] is not None else "",
               "current": number_text(figures["current"]) if figures["current"] is not None else "",
               "commentary": commentary}
        for cid in jev_ids:
            row[cid] = "fail" if cid in fails else "pass"
        row[REASON_COLUMN] = reason
        rows.append(row)
    if not rows:
        problems.warn("sample", "no labeled sample. Every new or reworded criterion stays Uncalibrated, so "
                                "its flags go to the reviewer as prompts until a sample is labeled")
    elif len(rows) < 15:
        problems.warn("sample", "{0} labeled lines; the interview aims for 15 to 20".format(len(rows)))
    check_sample_rows(rows, jev_ids, settings_rows, problems)
    return rows


def check_sample_rows(rows: Sequence[Dict[str, str]], jev_ids: Sequence[str],
                      settings_rows: Sequence[Dict[str, str]], problems: Problems) -> None:
    values = {r["Setting"]: r["Value"] for r in settings_rows}
    try:
        usd = parse_number(values.get(MATERIALITY_USD))
        pct = parse_number(values.get(MATERIALITY_PCT))
    except ValueError:
        usd = pct = None
    if pct is not None and pct > 1:
        pct /= 100.0
    seen: Dict[str, str] = {}
    for row in rows:
        key = normalize(row["commentary"]).lower()
        if key and key in seen:
            problems.warn("sample " + row["text_id"], "same commentary as {0}".format(seen[key]))
        seen.setdefault(key, row["text_id"])
        try:
            prior, current = parse_number(row["prior"]), parse_number(row["current"])
        except ValueError:
            continue
        if None not in (usd, pct, prior, current) and is_immaterial(prior, current, usd, pct):
            problems.warn("sample " + row["text_id"], "is under materiality, so the Coach would skip it in a "
                                                      "close; it still counts for calibration")


def readiness(jev_ids: Sequence[str], rows: Sequence[Dict[str, str]]) -> List[Readiness]:
    out = []
    for cid in jev_ids:
        fails = sum(1 for r in rows if (r.get(cid) or "").strip().lower() == "fail")
        passes = len(rows) - fails
        out.append(Readiness(cid, fails, passes,
                             held_out(fails) >= MIN_PER_CLASS and held_out(passes) >= MIN_PER_CLASS))
    return out


# ---------------------------------------------------------------------------
# Thresholds from calibrate.py
# ---------------------------------------------------------------------------


def apply_calibration(rows: List[Dict[str, str]], path: Path, problems: Problems) -> List[Tuple[str, str, str, str]]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.error("calibration", "can't read {0}: {1}".format(path, exc))
        return []
    if report.get("label_source") != "team":
        problems.warn("calibration", "{0} was calibrated on {1} labels, not the team's sample".format(
            path.name, report.get("label_source") or "unknown"))
    entries = {e.get("criterion_id"): e for e in report.get("criteria", []) if isinstance(e, dict)}
    applied = []
    for row in rows:
        cid = row["ID"]
        if cid in SYSTEM_IDS or row["Active (Y/N)"] != "Y":
            continue
        entry = entries.get(cid)
        where = "criterion " + cid
        if entry is None:
            problems.warn(where, "not in the calibration report; its threshold stays {0}".format(
                row["Threshold"] or "blank"))
            continue
        if entry.get("passes_when_hash") != passes_when_hash(row["Passes when"]):
            problems.error(where, "was calibrated on other wording of Passes when. Record and calibrate "
                                  "again after the last edit")
            continue
        try:
            threshold = float(entry["threshold"])
        except (KeyError, TypeError, ValueError):
            threshold = -1.0
        if not 0.0 < threshold < 1.0:
            problems.error(where, "the report's threshold is not between 0 and 1")
            continue
        row["Threshold"] = "{0:.2f}".format(threshold)
        row["Calibration status"] = "Calibrated" if str(entry.get("status")).lower() == "calibrated" \
            else "Uncalibrated"
        applied.append((cid, row["Threshold"], row["Calibration status"], str(entry.get("reason", ""))))
    return applied


# ---------------------------------------------------------------------------
# The three files, however they were made
# ---------------------------------------------------------------------------


def check_criteria_file(header: Sequence[str], rows: Sequence[Dict[str, str]],
                        defaults: Dict[str, Dict[str, str]], problems: Problems) -> List[str]:
    """Validate a Criteria tab file; returns the active Jev criterion IDs in order."""
    if list(header) != CRITERIA_COLUMNS:
        problems.error("criteria.csv", "columns must be exactly, in order: " + ", ".join(CRITERIA_COLUMNS))
        return []
    seen = set()
    jev_ids = []
    cleaned = []
    for number, raw in enumerate(rows, start=2):
        row = {c: normalize(raw.get(c)) for c in CRITERIA_COLUMNS}
        cid = row["ID"]
        if not cid:
            continue
        where = "criteria.csv row {0} ({1})".format(number, cid)
        if not ID_PATTERN.match(cid) or cid.upper() in RESERVED_IDS:
            problems.error(where, "not a usable ID")
            continue
        if cid in seen:
            problems.error(where, "ID appears more than once")
            continue
        seen.add(cid)
        if row["Active (Y/N)"] not in ("Y", "N"):
            problems.error(where, "Active (Y/N) must be Y or N")
        if row["Calibration status"] and row["Calibration status"] not in CALIBRATION_STATUSES:
            problems.error(where, "Calibration status must be one of " + ", ".join(CALIBRATION_STATUSES))
        if row["Last reviewed"] and not re.match(r"^\d{4}-\d{2}-\d{2}$", row["Last reviewed"]):
            problems.error(where, "Last reviewed must be a date like 2026-10-07")
        if cid in SYSTEM_IDS:
            if normalize(defaults[cid]["Passes when"]) != row["Passes when"]:
                problems.error(where, "{0} is a rule in code; keep its Passes when as shipped".format(cid))
            if row["Threshold"] != defaults[cid]["Threshold"]:
                problems.error(where, "{0}'s Threshold must stay {1!r}".format(cid, defaults[cid]["Threshold"]))
        else:
            threshold = row["Threshold"]
            if threshold:
                try:
                    value = parse_number(threshold)
                except ValueError:
                    value = None
                if value is None or not 0.0 < value < 1.0:
                    problems.error(where, "Threshold must be blank or between 0 and 1")
            if row["Active (Y/N)"] == "Y":
                shipped = cid in defaults and normalize(defaults[cid]["Passes when"]) == row["Passes when"]
                lint_jev_row(where, row, problems, shipped_wording=shipped)
                jev_ids.append(cid)
        cleaned.append(row)
    missing = [c for c in SYSTEM_IDS if c not in seen]
    if missing:
        problems.warn("criteria.csv", "no row for {0}: those checks will not run".format(", ".join(missing)))
    dropped = [c for c in defaults if c not in SYSTEM_IDS and c not in seen]
    if dropped:
        problems.warn("criteria.csv", "default criteria {0} have no row. If the sheet had them, keep them as "
                                      "rows with Active N so Criteria History records the retirement"
                                      .format(", ".join(dropped)))
    check_across(cleaned, problems)
    return jev_ids


def check_settings_file(header: Sequence[str], rows: Sequence[Dict[str, str]], problems: Problems) -> None:
    if list(header) != SETTINGS_COLUMNS:
        problems.error("settings.csv", "columns must be exactly, in order: " + ", ".join(SETTINGS_COLUMNS))
        return
    seen = set()
    for number, row in enumerate(rows, start=2):
        label = normalize(row.get("Setting"))
        if not label:
            continue
        where = "settings.csv row {0}".format(number)
        if label not in SETTING_KINDS:
            problems.error(where, "unknown setting {0!r}; the service would ignore this row".format(label))
            continue
        if label in seen:
            problems.error(where, "{0} appears more than once".format(label))
        seen.add(label)
        setting_value(label, row.get("Value"), problems)
    missing = [label for label in SETTING_KINDS if label not in seen]
    if missing:
        problems.warn("settings.csv", "no row for {0}; the service uses its defaults".format(", ".join(missing)))


def check_labels_file(header: Sequence[str], rows: Sequence[Dict[str, str]], jev_ids: Sequence[str],
                      settings_rows: Sequence[Dict[str, str]], problems: Problems) -> None:
    lead = list(header[:len(LABEL_COLUMNS)])
    middle = list(header[len(LABEL_COLUMNS):-1])
    if lead != LABEL_COLUMNS or not header or header[-1] != REASON_COLUMN:
        problems.error("labels.csv", "columns must be {0}, one column per active criterion, then {1}".format(
            ", ".join(LABEL_COLUMNS), REASON_COLUMN))
        return
    missing = [c for c in jev_ids if c not in middle]
    extra = [c for c in middle if c not in jev_ids]
    if missing:
        problems.error("labels.csv", "no label column for active criteria {0}".format(", ".join(missing)))
    if extra:
        problems.error("labels.csv", "label columns {0} are not active criteria".format(", ".join(extra)))
    seen = set()
    for number, row in enumerate(rows, start=2):
        where = "labels.csv row {0}".format(number)
        text_id = normalize(row.get("text_id"))
        if not text_id or text_id in seen:
            problems.error(where, "text_id is blank or repeated")
        seen.add(text_id)
        for name in ("prior", "current"):
            try:
                ok = parse_number(row.get(name)) is not None
            except ValueError:
                ok = False
            if not ok:
                problems.error(where, "{0} must be an amount".format(name))
        if not (row.get("commentary") or "").strip():
            problems.error(where, "commentary is empty")
        marks = [c for c in middle if (row.get(c) or "").strip().lower() == "fail"]
        for cid in middle:
            if (row.get(cid) or "").strip().lower() not in ("", "pass", "fail"):
                problems.error(where, "{0} must be pass, fail or blank (blank counts as pass)".format(cid))
        if marks and not normalize(row.get(REASON_COLUMN)):
            problems.error(where, "marks a failure without a reason")
    check_sample_rows(rows, jev_ids, settings_rows, problems)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_csv(path: Path, header: Sequence[str], rows: Sequence[Dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(header), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in header})


def readiness_lines(table: Sequence[Readiness]) -> List[str]:
    lines = ["| Criterion | Fail labels | Pass labels | Can calibrate? |", "| --- | --- | --- | --- |"]
    for item in table:
        if item.ready:
            verdict = "yes, if Jev's answers clear the accuracy floor"
        else:
            short = []
            if held_out(item.fails) < MIN_PER_CLASS:
                short.append("{0} more fail".format(LABELS_NEEDED - item.fails))
            if held_out(item.passes) < MIN_PER_CLASS:
                short.append("{0} more pass".format(LABELS_NEEDED - item.passes))
            verdict = "not yet: label {0}".format(" and ".join(short))
        lines.append("| {0} | {1} | {2} | {3} |".format(item.criterion_id, item.fails, item.passes, verdict))
    return lines


def summary_markdown(package: Dict[str, Any], package_path: Optional[Path], out: Path,
                     criteria_rows: Sequence[Dict[str, str]], origins: Dict[str, str],
                     changed: Dict[str, Tuple[str, str]], sample: Sequence[Dict[str, str]],
                     table: Sequence[Readiness], applied: Sequence[Tuple[str, str, str, str]],
                     problems: Problems) -> str:
    script = Path(__file__).resolve()
    lines = ["# Onboarding summary", ""]
    about = []
    if normalize(package.get("team")):
        about.append("Team: {0}.".format(normalize(package.get("team"))))
    if normalize(package.get("owner")):
        about.append("Criteria owner: {0}.".format(normalize(package.get("owner"))))
    if criteria_rows:
        about.append("Reviewed {0}.".format(criteria_rows[0]["Last reviewed"]))
    if about:
        lines += [" ".join(about), ""]
    lines += ["## Criteria", "", "| ID | Criterion | Origin | Active | Threshold | Calibration status |",
              "| --- | --- | --- | --- | --- | --- |"]
    for row in criteria_rows:
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} |".format(
            row["ID"], row["Criterion"], origins.get(row["ID"], ""), row["Active (Y/N)"],
            row["Threshold"] or "blank", row["Calibration status"]))
    if not applied:
        lines += ["", "Nothing here is calibrated yet. A default kept word for word starts from its shipped "
                      "threshold, which is a starting value, not a measurement. Until calibrate.py measures "
                      "a criterion on this team's sample, its flags go to the reviewer as prompts, not to "
                      "the preparer."]
    rejected = package.get("rejected") or []
    if rejected:
        lines += ["", "## Drafts rejected in the interview", ""]
        for item in rejected:
            if isinstance(item, dict):
                lines.append("- \"{0}\": {1}".format(normalize(item.get("draft")), normalize(item.get("why"))))
            else:
                lines.append("- " + normalize(item))
    lines += ["", "## Settings changed from the defaults", ""]
    lines += ["- {0}: {1} to {2}".format(k, a, b) for k, (a, b) in changed.items()] or ["- none"]
    lines += ["", "## Labeled sample", ""]
    if sample:
        lines.append("{0} lines. A criterion left unmarked on a line counts as a pass.".format(len(sample)))
        lines += [""] + readiness_lines(table)
        lines += ["", "calibrate.py holds out a quarter of each label and needs at least {0} passes and {0} "
                      "fails held out, so each criterion needs {1} or more lines of each. A criterion that "
                      "stays Uncalibrated still runs: its flags go to the reviewer as prompts instead of to "
                      "the preparer.".format(MIN_PER_CLASS, LABELS_NEEDED)]
        sources = [(r["text_id"], normalize(i.get("source"))) for r, i in zip(sample, package.get("sample") or [])
                   if isinstance(i, dict) and normalize(i.get("source"))]
        if sources:
            lines += ["", "Where the lines came from:", ""] + ["- {0}: {1}".format(t, s) for t, s in sources]
    else:
        lines.append("None. New and reworded criteria stay Uncalibrated until a sample is labeled.")
    if applied:
        lines += ["", "## Thresholds from calibration", ""]
        lines += ["- {0}: {1}, {2} ({3})".format(*a) for a in applied]
    if problems.warnings:
        lines += ["", "## Warnings", ""] + ["- " + w for w in problems.warnings]
    package_arg = str(package_path) if package_path else "package.json"
    lines += [
        "", "## Next steps", "",
        "From the root of the Coach repo. The first command sends each labeled line's account, comparison "
        "basis, prior and current amounts and commentary to Jev (through OpenRouter, or TypeSafe directly) "
        "and costs a fraction of a cent. Run it with `--dry-run` first to see what would be sent.",
        "",
        "```bash",
        "python scripts/record_jev.py --labels {0}/labels.csv --criteria {0}/criteria.csv "
        "--recordings {0}/team_recordings.jsonl --dry-run".format(out),
        "python scripts/record_jev.py --labels {0}/labels.csv --criteria {0}/criteria.csv "
        "--recordings {0}/team_recordings.jsonl --yes".format(out),
        "python scripts/calibrate.py --labels {0}/labels.csv --criteria {0}/criteria.csv "
        "--recordings {0}/team_recordings.jsonl --out {0}/team_calibration.json --write-db".format(out),
        "python {0} {1} --out {2} --calibration {2}/team_calibration.json".format(script, package_arg, out),
        "```",
        "",
        "Then in the sheet:",
        "",
        "1. Open the Criteria tab, then File > Import > Upload `criteria.csv`, and pick Import location "
        "\"Replace current sheet\".",
        "2. Do the same on the Settings tab with `settings.csv`.",
        "3. Coach > Admin > Set up sheet puts back the dropdowns and protections on the imported tabs.",
        "4. Coach > Sync criteria status shows which criteria the service counts as calibrated.",
    ]
    return "\n".join(lines) + "\n"


def print_report(problems: Problems, table: Sequence[Readiness]) -> None:
    for message in problems.errors:
        print("ERROR   " + message)
    for message in problems.warnings:
        print("warning " + message)
    if table:
        print("\n".join(readiness_lines(table)))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def write_package(package_path: Path, out: Path, calibration: Optional[Path]) -> int:
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("ERROR   {0}: {1}".format(package_path, exc))
        return 1
    problems = Problems()
    if not isinstance(package, dict):
        print("ERROR   the package is a JSON object")
        return 1
    unknown = sorted(set(package) - PACKAGE_KEYS)
    if unknown:
        problems.error("package", "unknown field(s) {0}".format(", ".join(unknown)))
    reviewed_on = normalize(package.get("reviewed_on")) or datetime.date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", reviewed_on):
        problems.error("reviewed_on", "must be a date like 2026-10-07")
    default_criteria, default_settings = load_defaults()
    criteria_rows, origins = build_criteria(package, default_criteria, reviewed_on, problems)
    settings_rows, changed = build_settings(package, default_settings, problems)
    sample = build_sample(package, criteria_rows, settings_rows, problems)
    applied = apply_calibration(criteria_rows, calibration, problems) if calibration else []
    jev_ids = [r["ID"] for r in criteria_rows if r["ID"] not in SYSTEM_IDS and r["Active (Y/N)"] == "Y"]
    table = readiness(jev_ids, sample) if sample else []
    if problems.errors:
        print_report(problems, [])
        print("Nothing written: {0} error(s).".format(len(problems.errors)))
        return 1

    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "criteria.csv", CRITERIA_COLUMNS, criteria_rows)
    write_csv(out / "settings.csv", SETTINGS_COLUMNS, settings_rows)
    if sample:
        write_csv(out / "labels.csv", LABEL_COLUMNS + jev_ids + [REASON_COLUMN], sample)
    elif (out / "labels.csv").exists():
        (out / "labels.csv").unlink()

    # What was written must pass the same checks a hand-made file would.
    again = Problems()
    check_files(out, default_criteria, again)
    if again.errors:
        print_report(again, [])
        print("ERROR   the written files fail their own checks; this is a bug in write_tabs.py")
        return 1

    (out / "onboarding_summary.md").write_text(summary_markdown(
        package, package_path, out, criteria_rows, origins, changed, sample, table, applied, problems),
        encoding="utf-8")
    print_report(problems, table)
    print("RAN: tabs written to {0} ({1} criteria, {2} settings, {3} labeled lines)".format(
        out, len(criteria_rows), len(settings_rows), len(sample)))
    return 0


def check_files(folder: Path, defaults: Dict[str, Dict[str, str]], problems: Problems) -> List[Readiness]:
    criteria_path, settings_path, labels_path = (folder / n for n in ("criteria.csv", "settings.csv", "labels.csv"))
    jev_ids: List[str] = []
    settings_rows: List[Dict[str, str]] = []
    if criteria_path.exists():
        header, rows = read_csv(criteria_path)
        jev_ids = check_criteria_file(header, rows, defaults, problems)
    else:
        problems.error(str(criteria_path), "missing")
    if settings_path.exists():
        header, settings_rows = read_csv(settings_path)
        check_settings_file(header, settings_rows, problems)
    else:
        problems.error(str(settings_path), "missing")
    if not labels_path.exists():
        problems.warn("labels.csv", "no labeled sample; new and reworded criteria stay Uncalibrated")
        return []
    header, rows = read_csv(labels_path)
    check_labels_file(header, rows, jev_ids, settings_rows, problems)
    return readiness(jev_ids, rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("package", type=Path, nargs="?", help="the onboarding package (JSON)")
    parser.add_argument("--out", type=Path, default=Path("onboarding"), help="folder for the written files")
    parser.add_argument("--calibration", type=Path, default=None,
                        help="calibrate.py's report: fills in Threshold and Calibration status")
    parser.add_argument("--check", type=Path, default=None, metavar="FOLDER",
                        help="validate criteria.csv, settings.csv and labels.csv in FOLDER instead")
    args = parser.parse_args(argv)
    if args.check is not None:
        problems = Problems()
        table = check_files(args.check, load_defaults()[0], problems)
        print_report(problems, table)
        if problems.errors:
            print("{0} error(s); fix them before importing.".format(len(problems.errors)))
            return 1
        print("RAN: {0} checked; ready to import".format(args.check))
        return 0
    if args.package is None:
        parser.error("give a package, or --check FOLDER")
    return write_package(args.package, args.out, args.calibration)


if __name__ == "__main__":
    sys.exit(main())
