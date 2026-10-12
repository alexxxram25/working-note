"""One reviewer edit changes what one preparer sees: five steps, with an assertion behind each.

  1. The reviewer rewrites C1's feedback in their own words. The pass
     condition does not change, so C1 stays calibrated.
  2. The preparer runs Coach on the September close and the hosting line fails C1.
  3. The reviewer's own feedback appears beside it, verbatim.
  4. The preparer rewrites the commentary.
  5. They rerun, and the same line passes, as attempt 2.

Then a probe that must hold: Jev is never asked about an immaterial line or
one that failed arithmetic or an exact duplicate.

The scenario runs over any transport: in-process (test_five_steps.py) or over
HTTP against a running service (test_deployment_path.py). Either way C1 has to
be calibrated for the sheet's wording first, or step 2 is a reviewer prompt
rather than a failure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import fernhill

NEW_C1_FEEDBACK = "Who or what, specifically? Name the customer, vendor, contract or hire so I can check it."


class InProcess:
    """The real app, called in-process, with real sheet-key auth."""

    def __init__(self, services, sheet_id: str) -> None:
        from fastapi.testclient import TestClient

        from coach.api import create_app
        from coach.auth import issue_key

        preparer_key, preparer = issue_key(sheet_id, "preparer", "five steps preparer")
        reviewer_key, reviewer = issue_key(sheet_id, "reviewer", "five steps reviewer")
        services.store.add_sheet_key(preparer)
        services.store.add_sheet_key(reviewer)
        self.client = TestClient(create_app(services=services))
        self.headers = {
            "preparer": {"X-Coach-Sheet-Key": preparer_key, "X-Coach-User": "preparer@example.com"},
            "reviewer": {"X-Coach-Sheet-Key": reviewer_key, "X-Coach-User": "reviewer@example.com"},
        }

    def call(self, method: str, path: str, role: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        response = self.client.request(method, path, json=body, headers=self.headers[role])
        return response.status_code, response.json()


class Remote:
    """A running service over HTTP, with one sheet key for both parts."""

    def __init__(self, url: str, key: str) -> None:
        import httpx

        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=120)
        self.key = key

    def call(self, method: str, path: str, role: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        headers = {"X-Coach-Sheet-Key": self.key, "X-Coach-User": "{0}@example.com".format(role)}
        response = self.http.request(method, path, json=body, headers=headers)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"detail": response.text[:300]}


class FiveSteps:
    def __init__(self, transport: Any, sheet_id: str) -> None:
        self.t = transport
        self.sheet_id = sheet_id
        self.criteria = [dict(r) for r in fernhill.criteria_rows()]
        self.settings = fernhill.settings_rows()
        self.runs: List[Dict[str, Any]] = []
        self.log: List[str] = []

    def run(self, lines, role: str = "preparer") -> Dict[str, Any]:
        status, body = self.t.call("POST", "/v1/runs", role, {
            "sheet_id": self.sheet_id, "criteria": self.criteria, "settings": self.settings,
            "lines": [line.model_dump() for line in lines]})
        assert status == 200, "run failed ({0}): {1}".format(status, body)
        self.runs.append(body)
        done, _ = self.t.call("POST", "/v1/runs/{0}/complete".format(body["run_id"]), role, {
            "sheet_id": self.sheet_id, "written": [r["line_id"] for r in body["results"]], "stale": []})
        assert done == 200, "could not complete run {0}".format(body["run_id"])
        return body

    def play(self) -> Dict[str, Any]:
        revision = fernhill.versions(fernhill.CLOSE)[fernhill.HOSTING_LINE][1]

        # 1. The reviewer rewrites C1's feedback; the pass condition is untouched.
        c1 = next(c for c in self.criteria if c["ID"] == "C1")
        c1["Feedback when failed"] = NEW_C1_FEEDBACK

        # 2. The preparer runs Coach and the hosting line fails C1.
        body = self.run(fernhill.lines_for(fernhill.CLOSE))
        # Versioning has its own tests; here the point is that a feedback edit keeps the calibration.
        status = {c["id"]: c for c in body["criteria_status"]}
        assert status["C1"]["calibration_status"] == "Calibrated", "C1 lost its calibration after a feedback edit"
        line = next(r for r in body["results"] if r["line_id"] == fernhill.HOSTING_LINE)
        assert line["status"] == "needs_revision" and line["failed"] == ["C1"], (line["status"], line["failed"])
        self.log.append("steps 1-2: {0} on {1}".format(line["status_label"], line["failed_cell"]))

        # 3. The reviewer's own words, verbatim.
        assert line["feedback_cell"] == "C1: " + NEW_C1_FEEDBACK, line["feedback_cell"]

        # 4 and 5. The preparer rewrites the line, reruns, and it passes as attempt 2.
        body = self.run(fernhill.lines_for(fernhill.CLOSE, {fernhill.HOSTING_LINE: revision}))
        line = next(r for r in body["results"] if r["line_id"] == fernhill.HOSTING_LINE)
        assert line["status"] == "passed", (line["status"], line["failed"])
        assert line["attempt_number"] == 2, line["attempt_number"]
        self.log.append("steps 3-5: {0}, attempt {1}".format(line["status_label"], line["attempt_number"]))
        self.probe()
        return body

    def probe(self) -> None:
        """Jev never sees an immaterial line or one that failed an objective check."""
        asked_wrongly = []
        for run in self.runs:
            for result in run["results"]:
                asked = any(c["tier"] == "jev" and c["result"] in ("pass", "fail", "prompt") for c in result["checks"])
                objective = any(c["criterion_id"] in ("ARITH", "DUP") and c["result"] == "fail"
                                for c in result["checks"])
                if asked and (not result["material"] or objective):
                    asked_wrongly.append(result["line_id"])
        assert not asked_wrongly, "Jev was asked about lines it must never see: {0}".format(asked_wrongly)
        immaterial = sum(1 for run in self.runs for r in run["results"] if not r["material"])
        assert immaterial, "the probe needs immaterial lines to mean anything"
        self.log.append("probe: 0 Jev questions on {0} immaterial line results".format(immaterial))
