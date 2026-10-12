"""The onboarding Skill: its copies of the service's rules, and the files it writes.

A team installs the Skill folder, not the repo, so the Skill carries its own
copies of the column orders, the settings, the default tabs, the materiality
gate and the calibration split. These tests hold each copy to the service's
original, read the files the Skill writes with the service's own parsers, and
push Fernhill's whole labeled set through the team path to show it
calibrates exactly as Fernhill's recorded calibration did.
"""

from __future__ import annotations

import copy
import csv
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import fernhill
from coach import labeled_set
from coach import models
from coach import sheet_schema as schema
from coach.api import _criteria
from coach.calibration import Example, Floors, split
from coach.checks.materiality import assess, variance_of
from coach.hashing import normalize_commentary
from coach.jev import questions
from coach.models import Criterion, Settings
from coach.seed import STARTER_CRITERIA, starter_criteria
from conftest import ROOT

SKILL = ROOT / "supporting" / "skills" / "working-note-onboarding"
PROMPT = ROOT / "supporting" / "onboarding_prompt.md"
EXAMPLE = SKILL / "assets" / "package_example.json"
JEV_DEFAULTS = ["C1", "C2", "C3", "C4", "C5", "RESTATE"]
SNAPSHOT = "typesafe/jev-1.13-20260917"


def _load(name: str):
    spec = importlib.util.spec_from_file_location("working_note_onboarding_" + name, SKILL / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


write_tabs = _load("write_tabs")
scan_history = _load("scan_history")


def _rows(path: Path):
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


# ---------------------------------------------------------------------------
# The Skill's copies of the service's rules
# ---------------------------------------------------------------------------


def test_skill_carries_the_starter_tabs():
    assert (SKILL / "assets" / "default_criteria.csv").read_bytes() == STARTER_CRITERIA.read_bytes(), (
        "copy backend/coach/starter_criteria.csv to {0}/assets/default_criteria.csv".format(SKILL.name))
    settings = ["Setting,Value,Notes"] + [
        ",".join('"{0}"'.format(v) if "," in v else v for v in (spec.label, str(spec.default), spec.notes))
        for spec in schema.SETTINGS_ROWS]
    assert (SKILL / "assets" / "default_settings.csv").read_text(encoding="utf-8") == "\n".join(settings) + "\n"


def test_prompt_carries_the_shipped_tabs_and_the_exact_headers():
    text = PROMPT.read_text(encoding="utf-8")
    blocks = re.findall(r"```csv\n(.*?)\n```", text, re.DOTALL)
    assert len(blocks) == 2
    for block, asset in zip(blocks, ("default_criteria.csv", "default_settings.csv")):
        assert block + "\n" == (SKILL / "assets" / asset).read_text(encoding="utf-8")
    assert "`" + ",".join(schema.CRITERIA_COLUMNS) + "`" in text
    assert "`" + ",".join(schema.SETTINGS_COLUMNS) + "`" in text
    assert "`" + ",".join(labeled_set.REQUIRED_COLUMNS) + "`" in text


def test_onboarding_prose_has_no_em_dashes():
    for path in (PROMPT, SKILL / "SKILL.md"):
        assert "—" not in path.read_text(encoding="utf-8"), path


def test_skill_rules_match_the_service():
    assert write_tabs.CRITERIA_COLUMNS == schema.CRITERIA_COLUMNS
    assert write_tabs.SETTINGS_COLUMNS == schema.SETTINGS_COLUMNS
    assert write_tabs.LABEL_COLUMNS == labeled_set.REQUIRED_COLUMNS
    assert list(write_tabs.SETTING_KINDS.items()) == [(s.label, s.kind) for s in schema.SETTINGS_ROWS]
    assert set(write_tabs.SYSTEM_IDS) == set(models.SYSTEM_KINDS)
    assert set(write_tabs.RESERVED_IDS) == {models.MATERIALITY_ID, models.COMMENTARY_ID}
    assert {b.lower() for b in write_tabs.KNOWN_BASES} == set(questions._BASIS_WORDS)
    assert write_tabs.MIN_PER_CLASS == Floors().min_holdout_per_class
    assert write_tabs.MAX_COMMENTARY == models.LineIn.model_fields["commentary"].metadata[0].max_length

    for text in ("  Names the customer ", "café au lait", "a\n\tb", None):
        assert write_tabs.normalize(text) == normalize_commentary(text)
        assert write_tabs.passes_when_hash(text) == questions.passes_when_hash(text)
    for value in ("$25,000", "(1,200)", "10%", 0.1, 7, "", "n/a", None):
        assert write_tabs.parse_number(value) == models.parse_number(value)

    for count in range(41):
        _, held = split([Example("t{0}".format(i), 0.5, True) for i in range(count)], "seed", write_tabs.HOLDOUT)
        assert write_tabs.held_out(count) == len(held), count
    assert write_tabs.LABELS_NEEDED == 6

    cases = [(100000, 120000), (100000, 109000), (100000, 110000), (0, 24999.99), (0, 25000), (250000, 274999),
             (250000, 275000), (-50000, -30000), (300000, 299000.004), (1, 1)]
    for prior, current in cases:
        expected = not assess(variance_of(prior, current), 25000, 0.10).material
        assert write_tabs.is_immaterial(prior, current, 25000, 0.10) == expected, (prior, current)


# ---------------------------------------------------------------------------
# Writing the tabs
# ---------------------------------------------------------------------------


def test_example_package_writes_tabs_the_service_reads(tmp_path, capsys):
    out = tmp_path / "onboarding"
    assert write_tabs.main([str(EXAMPLE), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "RAN: tabs written to" in printed and "(10 criteria, 17 settings, 8 labeled lines)" in printed

    header, rows = _rows(out / "criteria.csv")
    assert header == schema.CRITERIA_COLUMNS
    criteria = _criteria(rows)  # the API's own reading of a Criteria tab; raises on anything it refuses
    by_id = {c.id: c for c in criteria}
    assert [c.id for c in criteria if c.active] == ["C1", "C2", "C3", "C4", "C5", "C6", "ARITH", "DUP", "NEAR"]
    assert not by_id["RESTATE"].active
    assert all(c.last_reviewed == "2026-10-07" for c in criteria)

    shipped = {r["ID"]: Criterion.from_sheet_row(r) for r in starter_criteria()}
    # Kept word for word: the same criterion version, and the shipped calibration still applies.
    for cid in ("C1", "C3", "C4", "ARITH", "DUP", "NEAR"):
        assert by_id[cid].sheet_content() == shipped[cid].sheet_content(), cid
    assert questions.calibration_fingerprint(SNAPSHOT, by_id["C1"].passes_when, by_id["C1"].threshold) == \
        questions.calibration_fingerprint(SNAPSHOT, shipped["C1"].passes_when, shipped["C1"].threshold)
    # Feedback edited, pass condition kept: a new version, but the threshold carries.
    assert by_id["C2"].feedback != shipped["C2"].feedback and by_id["C2"].threshold == shipped["C2"].threshold
    # Reworded or new: no threshold until the team's sample is calibrated.
    assert by_id["C5"].threshold is None and by_id["C6"].threshold is None
    assert {r["ID"]: r["Calibration status"] for r in rows}["C6"] == "Uncalibrated"

    header, settings_rows = _rows(out / "settings.csv")
    assert header == schema.SETTINGS_COLUMNS
    assert [r["Setting"] for r in settings_rows] == [s.label for s in schema.SETTINGS_ROWS]
    settings = Settings.from_sheet_rows(settings_rows)
    assert (settings.materiality_usd, settings.materiality_pct) == (50000, 0.05)
    assert settings.min_sample_size == Settings().min_sample_size

    package = json.loads(EXAMPLE.read_text())
    jev_ids = [c.id for c in criteria if c.kind == "jev" and c.active]
    items = labeled_set.from_csv(out / "labels.csv", jev_ids)
    assert [i.text_id for i in items] == [s["text_id"] for s in package["sample"]]
    for item, line in zip(items, package["sample"]):
        assert {cid for cid, label in item.labels.items() if label == "fail"} == set(line["fails"])
        assert item.line.commentary == line["commentary"]
        assert (item.line.prior, item.line.current) == (line["prior"], line["current"])

    summary = (out / "onboarding_summary.md").read_text()
    assert "Commentary is clear and professional" in summary
    assert "Materiality ($): 25000 to 50000" in summary
    assert "| C6 | 4 | 4 | not yet: label 2 more fail and 2 more pass |" in summary
    assert "scripts/record_jev.py --labels" in summary and "--dry-run" in summary

    assert write_tabs.main(["--check", str(out)]) == 0
    assert "RAN: " in capsys.readouterr().out


def test_team_path_calibrates_exactly_like_fernhills_recorded_calibration(tmp_path):
    """Fernhill's whole labeled set, as a team package: same thresholds, statuses and metrics."""
    flux = {r["Line ID"]: r for close in fernhill.CLOSES for r in fernhill.flux_rows(close)}
    sample = []
    for label in fernhill.labels():
        row = flux[label["line_id"]]
        fails = [c for c in JEV_DEFAULTS if label[c] == "fail"]
        sample.append({"text_id": label["text_id"], "account": row["Account"],
                       "comparison_basis": row["Comparison basis"], "prior": row["Prior"], "current": row["Current"],
                       "commentary": label["commentary"], "fails": fails,
                       "reason": (label["note"] or "Labeled with Fernhill's set.") if fails else ""})
    package = {"owner": "Controller", "reviewed_on": "2026-09-08",
               "criteria": [{"id": c, "from_default": True} for c in JEV_DEFAULTS],
               "settings": {"Materiality ($)": 25000, "Materiality (%)": 0.10}, "sample": sample}
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package))
    out = tmp_path / "team"
    assert write_tabs.main([str(path), "--out", str(out)]) == 0

    report_path = out / "team_calibration.json"
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "calibrate.py"), "--labels", str(out / "labels.csv"),
                             "--criteria", str(out / "criteria.csv"), "--recordings", str(fernhill.RECORDINGS),
                             "--out", str(report_path)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    team = json.loads(report_path.read_text())
    shipped = json.loads((fernhill.DIR / "calibration_report.json").read_text())
    assert team["label_source"] == "team" and team["labeled_texts"] == len(sample)
    assert team["labeled_set_hash"] == shipped["labeled_set_hash"]

    def outcome(report):
        return [(c["criterion_id"], c["threshold"], c["status"], c["fingerprint"], c["metrics"])
                for c in report["criteria"]]

    assert outcome(team) == outcome(shipped)

    assert write_tabs.main([str(path), "--out", str(out), "--calibration", str(report_path)]) == 0
    tab = {r["ID"]: r for r in _rows(out / "criteria.csv")[1]}
    for entry in shipped["criteria"]:
        assert tab[entry["criterion_id"]]["Threshold"] == "{0:.2f}".format(entry["threshold"])
        assert tab[entry["criterion_id"]]["Calibration status"] == entry["status"]
    assert "## Thresholds from calibration" in (out / "onboarding_summary.md").read_text()


def _base_package():
    return {
        "owner": "Controller", "reviewed_on": "2026-10-07",
        "criteria": [{"id": c, "from_default": True} for c in JEV_DEFAULTS],
        "settings": {"Materiality ($)": 25000, "Materiality (%)": 0.10},
        "sample": [{"text_id": "S01", "account": "6270 Paid search", "prior": 96000, "current": 71000,
                    "commentary": "Decrease due to lower spend.", "fails": ["C1", "C2"],
                    "reason": "No driver and nothing about the business."}],
    }


NEW = {"id": "C6", "criterion": "Dates the driver", "passes_when": "Says when the driver happened",
       "feedback": "When did this happen?", "question": "Does the commentary say when the driver happened?"}


def _with_new(**fields):
    return lambda p: p["criteria"].append(dict(NEW, **fields))


def _sample(**fields):
    return lambda p: p["sample"][0].update(fields)


REFUSED = [
    (_with_new(question="What drove the change?"), "is not a yes-or-no question"),
    (_with_new(question=""), "no yes-or-no question"),
    (_with_new(passes_when="The explanation is accurate"), "'accurate' asks whether the explanation is true"),
    (_with_new(passes_when="Says what is right about the driver"), "'right' asks whether"),
    (_with_new(passes_when="Scores at least 4 out of 5 for clarity"), "makes this a score"),
    (_with_new(passes_when="Does it name the customer?"), "not a question"),
    (_with_new(feedback="Name [customer] and the contract."), "placeholder"),
    (_with_new(feedback=""), "Feedback when failed is empty"),
    (_with_new(criterion=""), "Criterion (the short name) is empty"),
    (_with_new(colour="blue"), "unknown field(s) colour"),
    (_with_new(id="MATERIALITY"), "the service's name for its own results"),
    (_with_new(id="C|6"), "an ID starts with a letter"),
    (lambda p: p["criteria"].append({"id": "C1", "from_default": True}), "appears more than once"),
    (lambda p: p["criteria"].pop(3), "criterion C4: is in the default set but not in the package"),
    (lambda p: p["criteria"].append({"id": "ARITH", "passes_when": "Adds up"}), "is a rule in code"),
    (lambda p: p["criteria"].append({"id": "C9", "from_default": True}), "is not a default criterion"),
    (lambda p: p["criteria"].append({"id": "C6", "active": "maybe", **{k: v for k, v in NEW.items() if k != "id"}}),
     "active must be true or false"),
    (lambda p: p.update(criteria=[]), "list the criteria"),
    (lambda p: p["settings"].pop("Materiality (%)"), "Materiality (%): not confirmed"),
    (lambda p: p.update(settings=None), "record the materiality"),
    (lambda p: p["settings"].update({"Materiality $": 1}), "unknown setting 'Materiality $'"),
    (lambda p: p["settings"].update({"Materiality (%)": 150}), "must be between 0 and 1"),
    (lambda p: p["settings"].update({"Materiality ($)": "lots"}), "is not a number"),
    (lambda p: p["settings"].update({"Materiality ($)": -5}), "must not be negative"),
    (lambda p: p["settings"].update({"Minimum sample size": 2.5}), "must be a whole number"),
    (lambda p: p["settings"].update({"Pinned model version": "latest"}), "must look like jev-1.13.0"),
    (_sample(fails=["ARITH"]), "ARITH is checked by code"),
    (_sample(fails=["C9"]), "marks C9, which is not a criterion in the package"),
    (lambda p: (p["criteria"][5].update(active=False), p["sample"][0].update(fails=["RESTATE"])),
     "RESTATE is retired"),
    (_sample(reason=""), "marks a failure without a reason"),
    (_sample(prior="abc"), "prior must be an amount"),
    (_sample(current=None), "current must be an amount"),
    (_sample(commentary="  "), "commentary is empty"),
    (_sample(account=""), "account is empty"),
    (_sample(comparison_basis="MoM|x"), "can't contain '|'"),
    (_sample(note="x"), "unknown field(s) note"),
    (lambda p: p["sample"].append(dict(p["sample"][0])), "text_id appears more than once"),
    (lambda p: p.update(sample={"S01": {}}), "the labeled sample is a list"),
    (lambda p: p.update(reviewed_on="10/07/2026"), "must be a date like"),
    (lambda p: p.update(extra=True), "package: unknown field(s) extra"),
]


@pytest.mark.parametrize("change, message", REFUSED, ids=[m for _, m in REFUSED])
def test_problems_are_refused_and_nothing_is_written(tmp_path, capsys, change, message):
    package = _base_package()
    change(package)
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package))
    out = tmp_path / "out"
    assert write_tabs.main([str(path), "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert message in printed, printed
    assert "Nothing written" in printed
    assert not out.exists()


def test_judgment_words_warn_without_refusing_and_finance_terms_pass(tmp_path, capsys):
    package = _base_package()
    package["criteria"] += [
        dict(NEW, passes_when="Gives a clear account of the driver"),
        dict(NEW, id="C7", passes_when="Names the true-up and the right-of-use asset it relates to",
             question="Does the commentary name the true-up and the asset?", feedback="Name the asset."),
    ]
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package))
    assert write_tabs.main([str(path), "--out", str(tmp_path / "out")]) == 0
    printed = capsys.readouterr().out
    assert "criterion C6: 'clear' is a judgment two reviewers could make differently" in printed
    assert "criterion C7" not in printed
    assert "1 labeled lines; the interview aims for 15 to 20" in printed


def test_shipped_wording_skips_the_wording_lints_and_an_edit_brings_them_back(tmp_path, capsys):
    _, rows = _rows(SKILL / "assets" / "default_criteria.csv")
    shipped = next(r for r in rows if r["ID"] == "C1")["Passes when"]
    # The probe: the shipped C1 would trip both the old 300-character rule and the 'enough' warning.
    assert len(shipped) > 300 and "enough" in shipped
    package = _base_package()
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package))
    assert write_tabs.main([str(path), "--out", str(tmp_path / "kept")]) == 0
    assert "criterion C1" not in capsys.readouterr().out
    package["criteria"][0] = {"id": "C1", "from_default": True, "passes_when": shipped.replace("look it up", "find it"),
                              "question": "Does the commentary name the specific cause of the change?"}
    path.write_text(json.dumps(package))
    assert write_tabs.main([str(path), "--out", str(tmp_path / "edited")]) == 0
    assert "criterion C1: 'enough' is a judgment two reviewers could make differently" in capsys.readouterr().out


@pytest.mark.parametrize("extra, refused", [(0, False), (72, True)])
def test_a_pass_condition_has_room_for_what_does_not_count(tmp_path, capsys, extra, refused):
    condition = ("Names the customer, contract or hire behind the change. Fails when the cause is a category "
                 "(\"timing\", \"growth\", \"higher volume\"), even with an amount or dates attached, or when the only "
                 "name given is the vendor that billed the account or the account it was booked to; a report to "
                 "look in is not a cause either, and neither is a department.") + " Really." * (extra // 8)
    assert 300 < len(condition) <= 400 if not refused else len(condition) > 400  # over the old cap, or the new one
    package = _base_package()
    package["criteria"].append(dict(NEW, passes_when=condition))
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package))
    assert write_tabs.main([str(path), "--out", str(tmp_path / "out")]) == (1 if refused else 0)
    printed = capsys.readouterr().out
    assert ("keep it to one condition and what doesn't count" in printed) == refused


def test_a_package_without_a_sample_writes_tabs_and_says_what_that_means(tmp_path, capsys):
    package = _base_package()
    package.pop("sample")
    package["criteria"][4] = {"id": "C5", "from_default": True, "active": False}
    package["criteria"].append({"id": "NEAR", "active": False})
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package))
    out = tmp_path / "out"
    out.mkdir()
    (out / "labels.csv").write_text("stale from an earlier run\n")
    assert write_tabs.main([str(path), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "no labeled sample" in printed and "retired: the not a template check will not run" in printed
    assert not (out / "labels.csv").exists()
    ids = [(r["ID"], r["Active (Y/N)"]) for r in _rows(out / "criteria.csv")[1]]
    assert ids == [("C1", "Y"), ("C2", "Y"), ("C3", "Y"), ("C4", "Y"), ("RESTATE", "Y"), ("ARITH", "Y"),
                   ("DUP", "Y"), ("C5", "N"), ("NEAR", "N")]
    assert "None. New and reworded criteria stay Uncalibrated" in (out / "onboarding_summary.md").read_text()


def test_calibration_report_must_match_the_wording(tmp_path, capsys):
    package = _base_package()
    package["criteria"].append(dict(NEW))
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package))
    report = {"label_source": "team", "criteria": [
        {"criterion_id": "C6", "threshold": 0.62, "status": "calibrated", "reason": "held-out 0.90",
         "passes_when_hash": questions.passes_when_hash(NEW["passes_when"])},
    ]}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    out = tmp_path / "out"
    assert write_tabs.main([str(path), "--out", str(out), "--calibration", str(report_path)]) == 0
    assert {r["ID"]: (r["Threshold"], r["Calibration status"]) for r in _rows(out / "criteria.csv")[1]}["C6"] == \
        ("0.62", "Calibrated")
    assert "criterion C1: not in the calibration report" in capsys.readouterr().out

    report["criteria"][0]["passes_when_hash"] = questions.passes_when_hash("Says when it happened")
    report["label_source"] = "synthetic"
    report_path.write_text(json.dumps(report))
    assert write_tabs.main([str(path), "--out", str(tmp_path / "out2"), "--calibration", str(report_path)]) == 1
    printed = capsys.readouterr().out
    assert "was calibrated on other wording of Passes when" in printed
    assert "calibrated on synthetic labels, not the team's sample" in printed

    report_path.write_text("{not json")
    assert write_tabs.main([str(path), "--out", str(tmp_path / "out3"), "--calibration", str(report_path)]) == 1


def test_unreadable_package(tmp_path, capsys):
    path = tmp_path / "package.json"
    path.write_text("[1, 2]")
    assert write_tabs.main([str(path)]) == 1
    path.write_text("{")
    assert write_tabs.main([str(path)]) == 1
    with pytest.raises(SystemExit):
        write_tabs.main([])


# ---------------------------------------------------------------------------
# --check: files from the chat prompt
# ---------------------------------------------------------------------------


@pytest.fixture
def prompt_output(tmp_path):
    """What the chat prompt is asked to write: the shipped tabs and a two-line sample."""
    folder = tmp_path / "from_chat"
    folder.mkdir()
    for asset, name in (("default_criteria.csv", "criteria.csv"), ("default_settings.csv", "settings.csv")):
        (folder / name).write_bytes((SKILL / "assets" / asset).read_bytes())
    (folder / "labels.csv").write_text(
        "text_id,account,comparison_basis,prior,current,commentary,C1,C2,C3,C4,C5,RESTATE,reason\n"
        "S01,6270 Paid search,MoM,96000,71000,Decrease due to lower spend.,fail,fail,fail,fail,fail,fail,"
        "Says nothing the row doesn't.\n"
        "S02,6330 Audit and tax fees,MoM,45000,72000,\"Up $27K: Calder & Moss billed the Q2 review ($19K).\","
        ",,,,,,\n")
    return folder


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def test_check_accepts_prompt_output(prompt_output, capsys):
    assert write_tabs.main(["--check", str(prompt_output)]) == 0
    printed = capsys.readouterr().out
    assert "RAN: {0} checked; ready to import".format(prompt_output) in printed
    assert "| C1 | 1 | 1 | not yet: label 5 more fail and 5 more pass |" in printed


CHECK_REFUSED = [
    ("criteria.csv", "ID,Criterion,Passes when", "ID,Passes when,Criterion", "columns must be exactly, in order"),
    ("criteria.csv", ",Settings,Heuristic,Y", ",0.5,Heuristic,Y", "NEAR's Threshold must stay 'Settings'"),
    ("criteria.csv", "(rule in code; not editable here)", "(edited)", "is a rule in code; keep its Passes when"),
    ("criteria.csv", "Controller,,0.43,Uncalibrated,Y", "Controller,,1.5,Uncalibrated,Y",
     "Threshold must be blank or between 0 and 1"),
    ("criteria.csv", "Controller,,0.43,Uncalibrated,Y", "Controller,Sept 8,0.43,Uncalibrated,Y",
     "Last reviewed must be a date"),
    ("criteria.csv", "Controller,,0.43,Uncalibrated,Y", "Controller,,0.43,Great,Y",
     "Calibration status must be one of"),
    ("criteria.csv", "Controller,,0.43,Uncalibrated,Y", "Controller,,0.43,Uncalibrated,yes",
     "Active (Y/N) must be Y or N"),
    ("criteria.csv", "\nC2,", "\nC1,", "ID appears more than once"),
    ("criteria.csv", "\nC2,", "\nMATERIALITY,", "not a usable ID"),
    ("settings.csv", "Materiality ($),25000", "Materiality $,25000", "unknown setting 'Materiality $'"),
    ("settings.csv", "Materiality ($),25000", "Materiality (%),25000", "Materiality (%) appears more than once"),
    ("settings.csv", "Setting,Value,Notes", "Value,Setting,Notes", "columns must be exactly, in order: Setting"),
    ("labels.csv", ",RESTATE,reason", ",reason", "no label column for active criteria RESTATE"),
    ("labels.csv", ",RESTATE,reason", ",RESTATE,C9,reason", "label columns C9 are not active criteria"),
    ("labels.csv", "text_id,account", "id,account", "columns must be text_id"),
    ("labels.csv", ",fail,fail,fail,fail,fail,fail,", ",maybe,fail,fail,fail,fail,fail,", "C1 must be pass, fail or blank"),
    ("labels.csv", "Says nothing the row doesn't.", "", "marks a failure without a reason"),
    ("labels.csv", "S02,", "S01,", "text_id is blank or repeated"),
    ("labels.csv", ",96000,", ",ninety,", "prior must be an amount"),
    ("labels.csv", "Decrease due to lower spend.", "", "commentary is empty"),
]


@pytest.mark.parametrize("name, old, new, message", CHECK_REFUSED, ids=[c[3] for c in CHECK_REFUSED])
def test_check_refuses_files_the_service_would_misread(prompt_output, capsys, name, old, new, message):
    _edit(prompt_output / name, old, new)
    assert write_tabs.main(["--check", str(prompt_output)]) == 1
    printed = capsys.readouterr().out
    assert message in printed, printed
    assert "fix them before importing" in printed


def test_check_reports_missing_files_and_rows(tmp_path, prompt_output, capsys):
    assert write_tabs.main(["--check", str(tmp_path / "nowhere")]) == 1
    printed = capsys.readouterr().out
    assert "criteria.csv: missing" in printed and "settings.csv: missing" in printed

    (prompt_output / "labels.csv").unlink()
    _edit(prompt_output / "criteria.csv", "\nC5,", "\nC5X,")
    lines = (prompt_output / "settings.csv").read_text().splitlines()
    (prompt_output / "settings.csv").write_text("\n".join(lines[:-1]) + "\n")
    lines = (prompt_output / "criteria.csv").read_text().splitlines()
    (prompt_output / "criteria.csv").write_text("\n".join(l for l in lines if not l.startswith("DUP,")) + "\n")
    assert write_tabs.main(["--check", str(prompt_output)]) == 0
    printed = capsys.readouterr().out
    assert "no row for DUP: those checks will not run" in printed
    assert "default criteria C5 have no row" in printed
    assert "no row for Arithmetic tolerance (%); the service uses its defaults" in printed
    assert "labels.csv: no labeled sample" in printed


# ---------------------------------------------------------------------------
# The labeled-set reader the team path relies on
# ---------------------------------------------------------------------------


def _labels(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "labels.csv"
    path.write_text("text_id,account,comparison_basis,prior,current,commentary,C1,C2,reason\n" + body)
    return path


def test_from_csv_reads_a_team_sample(tmp_path):
    path = _labels(tmp_path, "S01,6270 Paid search,,96000,71000,Lower spend.,fail,,No driver.\n"
                             "S02,6270 Paid search,QoQ,\"$96,000\",(71000),Paused the campaign.,PASS,Fail,x\n")
    first, second = labeled_set.from_csv(path, ["C1", "C2"])
    assert first.labels == {"C1": "fail", "C2": "pass"}  # unmarked counts as a pass
    assert first.line.basis == "MoM" and first.line.line_id == "TEAM|label|MoM|2"
    assert second.labels == {"C1": "pass", "C2": "fail"}
    assert (second.line.prior, second.line.current) == (96000.0, -71000.0)


@pytest.mark.parametrize("body, criteria, message", [
    ("S01,a,MoM,1,2,x,fail,,r\n", ["C1", "C3"], "missing columns: C3"),
    ("S01,a,MoM,1,2,x,fail,,r\nS01,b,MoM,1,2,y,,,\n", ["C1", "C2"], ":3: text_id is blank or repeated"),
    (",a,MoM,1,2,x,fail,,r\n", ["C1", "C2"], ":2: text_id is blank or repeated"),
    ("S01,a,MoM,1,2,x,maybe,,r\n", ["C1", "C2"], ":2: C1 must be pass, fail or blank"),
    ("", ["C1", "C2"], "has no labeled lines"),
])
def test_from_csv_refuses_what_calibration_cannot_use(tmp_path, body, criteria, message):
    with pytest.raises(labeled_set.LabeledSetError, match=re.escape(message)):
        labeled_set.from_csv(_labels(tmp_path, body), criteria)


# ---------------------------------------------------------------------------
# Reading a folder of past closes
# ---------------------------------------------------------------------------


def test_scan_history_finds_every_flux_line_once(tmp_path, capsys):
    out = tmp_path / "candidates.csv"
    assert scan_history.main([str(fernhill.DIR), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    header, rows = _rows(out)
    assert header == scan_history.COLUMNS
    expected = {normalize_commentary(r["Commentary"]).lower()
                for close in fernhill.CLOSES for r in fernhill.flux_rows(close) if r["Commentary"].strip()}
    assert {normalize_commentary(r["commentary"]).lower() for r in rows} == expected
    assert len(rows) == len(expected)
    assert rows[0]["source"] == "flux_2026-07.csv row 2" and rows[0]["candidate_id"] == "H0001"
    assert (rows[0]["comparison_basis"], rows[0]["change"], rows[0]["change_pct"]) == ("MoM", "188000", "4.5%")
    assert "workbook.xlsx > Flux" in printed  # the workbook repeats a close: counted, not re-listed
    assert "RAN: {0} candidate line(s)".format(len(expected)) in printed


def test_scan_history_on_a_messy_folder(tmp_path, capsys):
    folder = tmp_path / "history"
    (folder / "2025").mkdir(parents=True)
    (folder / "2025" / "bva.csv").write_text(
        "Fernhill budget review,,,\n"
        "GL Account,Budget,Actual,Variance explanation\n"
        "6270 Paid search,\"$96,000\",\"$71,000\",Paused the campaign.\n"
        "6280 Webinars,,40000,Two webinars.\n"
        "6290 Content,10000,12000,\n")
    (folder / "2025" / "py.tsv").write_text("Account\tPY\tCY\tComments\n4000 Revenue\t100\t(50)\tLower.\n")
    (folder / "notes.pdf").write_bytes(b"%PDF-1.4")
    (folder / "broken.xlsx").write_bytes(b"not a workbook")
    (folder / ".hidden.csv").write_text("Account,Prior,Current,Commentary\nx,1,2,y\n")
    out = tmp_path / "candidates.csv"
    assert scan_history.main([str(folder), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    rows = {r["account"]: r for r in _rows(out)[1]}
    assert set(rows) == {"6270 Paid search", "4000 Revenue"}
    assert rows["6270 Paid search"]["comparison_basis"] == "vs Budget"
    assert rows["6270 Paid search"]["source"] == "bva.csv row 3"
    assert rows["4000 Revenue"]["comparison_basis"] == "YoY" and rows["4000 Revenue"]["current"] == "-50"
    assert "read   notes.pdf" in printed and "skip   broken.xlsx" in printed
    assert "1 line(s) had commentary but not both amounts" in printed
    assert scan_history.main([str(tmp_path / "missing")]) == 1
