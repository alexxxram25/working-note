"""The five steps in-process: real API, memory store, real sheet-key auth, recorded Jev answers."""

from __future__ import annotations

import fernhill
from five_steps import FiveSteps, InProcess


def test_one_reviewer_edit_reaches_one_preparer():
    services = fernhill.services(auth_modes=("sheet_key",), rate_limit_per_minute=0)
    steps = FiveSteps(InProcess(services, "five-steps"), "five-steps")
    steps.play()
    print("\n".join("RAN: five steps, " + line for line in steps.log))


def test_before_calibration_the_same_flag_is_a_prompt_for_the_reviewer():
    """A fresh deployment calibrates nothing: C1's flag reaches the reviewer, not the preparer."""
    services = fernhill.services(auth_modes=("sheet_key",), rate_limit_per_minute=0)
    services.store._calibrations.clear()
    steps = FiveSteps(InProcess(services, "uncalibrated"), "uncalibrated")
    body = steps.run(fernhill.lines_for(fernhill.CLOSE))
    status = {c["id"]: c["calibration_status"] for c in body["criteria_status"]}
    assert {status[c] for c in fernhill.JEV_CRITERIA} == {"Uncalibrated"}
    line = next(r for r in body["results"] if r["line_id"] == fernhill.HOSTING_LINE)
    assert line["status"] != "needs_revision" and line["failed"] == [], (line["status"], line["failed"])
    c1 = next(c for c in line["checks"] if c["criterion_id"] == "C1")
    assert c1["result"] == "prompt", c1
    print("RAN: uncalibrated C1 is a reviewer prompt: {0}".format(line["status_label"]))
