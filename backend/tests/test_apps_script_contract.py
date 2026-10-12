"""Code.gs and the Python schema must name every column the same way.

The script cannot import Python, so it carries its own copy of the headers.
This runs the real Code.gs under Node and compares what it declares with
backend/coach/sheet_schema.py. A renamed header fails here instead of writing
results into the wrong column on someone's sheet.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coach import sheet_schema as schema
from conftest import ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture(scope="module")
def constants():
    result = subprocess.run([NODE, str(ROOT / "apps_script" / "test" / "print_constants.mjs")],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_tab_names_and_order(constants):
    assert constants["TAB_ORDER"] == schema.TABS


@pytest.mark.parametrize("js,py", [
    ("FLUX_COLUMNS", "FLUX_COLUMNS"),
    ("CRITERIA_COLUMNS", "CRITERIA_COLUMNS"),
    ("SETTINGS_COLUMNS", "SETTINGS_COLUMNS"),
    ("LOG_COLUMNS", "LOG_COLUMNS"),
    ("CRITERIA_HISTORY_COLUMNS", "CRITERIA_HISTORY_COLUMNS"),
    ("CRITERIA_HEALTH_COLUMNS", "CRITERIA_HEALTH_COLUMNS"),
    ("SECOND_ATTEMPTS_LEAD_COLUMNS", "SECOND_ATTEMPTS_LEAD_COLUMNS"),
    ("HEALTH_RESOLUTIONS", "HEALTH_RESOLUTIONS"),
])
def test_columns_match(constants, js, py):
    assert constants[js] == getattr(schema, py)


def test_every_status_the_service_returns_has_a_style(constants):
    from coach import feedback

    assert set(feedback.STATUS_LABELS) <= set(constants["STATUS_STYLES"])


def test_manifest_scopes_are_exactly_what_the_readme_discloses():
    manifest = json.loads((ROOT / "apps_script" / "appsscript.json").read_text())
    assert sorted(manifest["oauthScopes"]) == sorted([
        "https://www.googleapis.com/auth/spreadsheets.currentonly",
        "https://www.googleapis.com/auth/script.external_request",
        "https://www.googleapis.com/auth/script.container.ui",
        "https://www.googleapis.com/auth/userinfo.email",
        "openid",
    ])
    assert manifest["runtimeVersion"] == "V8"
    assert all(url.startswith("https://") and url.endswith("/") for url in manifest["urlFetchWhitelist"])
    assert "@OnlyCurrentDoc" in (ROOT / "apps_script" / "Code.gs").read_text()
