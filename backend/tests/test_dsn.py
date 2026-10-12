"""A malformed connection string must never be repeated back, in any error.

The probe below is the reason this module exists: psycopg, handed a URI whose
password has an unencoded %, raises an error that quotes the password. Every
connection goes through coach.dsn.connect, which must refuse such a string in
its own words and scrub any error that still mentions the password.
"""

from __future__ import annotations

import os
import subprocess
import sys

import psycopg
import pytest

from coach import dsn
from coach.config import Config
from conftest import ROOT

PASSWORD = "pw$Tz%9K&secret"
LEAKY = "postgresql://postgres.abcdefghijklmnop:{0}@aws-0-us-east-1.pooler.supabase.com:6543/postgres".format(PASSWORD)
FRAGMENTS = [PASSWORD, "Tz%9K&secret", "%9K&secret"]


def _clean(text: str) -> bool:
    return not any(fragment in text for fragment in FRAGMENTS)


def test_probe_psycopg_itself_quotes_the_password():
    with pytest.raises(psycopg.ProgrammingError) as caught:
        psycopg.connect(LEAKY)
    assert not _clean(str(caught.value)), "psycopg no longer echoes it; the guard is still worth keeping"


@pytest.mark.parametrize("value, message", [
    (LEAKY, "a % that isn't percent-encoded"),
    (LEAKY.replace(PASSWORD, "[abcdefgh]"), "brackets from the [YOUR-PASSWORD]"),
    (LEAKY.replace(PASSWORD, "ab@cdefgh"), "contains an @"),
    (LEAKY.replace(PASSWORD, "ab/cdefgh"), "/ ? or #"),
    (LEAKY + " ", "spaces or quotes"),
    ('"' + LEAKY + '"', "spaces or quotes"),
    (LEAKY.replace("6543/", "6543/ "), "spaces or quotes"),
    ("", "empty"),
])
def test_problems_are_described_without_quoting_the_string(value, message):
    found = dsn.problems(value)
    assert any(message in p for p in found), found
    assert all(_clean(p) and "abcdefgh" not in p for p in found)


def test_well_formed_strings_pass():
    assert dsn.problems(LEAKY.replace(PASSWORD, "pw%24Tz%259K%26secret")) == []
    assert dsn.problems("postgresql://postgres@localhost:54329/coach_test") == []
    assert dsn.problems("host=localhost port=5432 dbname=x user=y password=p%q") == []


def test_connect_refuses_and_hides_the_password():
    with pytest.raises(dsn.DsnError) as caught:
        dsn.connect(LEAKY)
    assert _clean(str(caught.value)) and "isn't percent-encoded" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None  # refused before psycopg saw it


def test_a_parse_error_libpq_finds_is_replaced_not_chained():
    value = "postgresql://someone:quietpassword9@localhost:1/db?not_a_real_option=1"
    assert dsn.problems(value) == []
    with pytest.raises(dsn.DsnError) as caught:
        dsn.connect(value, connect_timeout=1)
    assert "quietpassword9" not in str(caught.value) and dsn.REDACTED in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_redact_catches_fragments():
    assert dsn.redact("bad token: \"Tz%9K&secret\"", LEAKY) == dsn.REDACTED
    assert dsn.redact("FATAL: password authentication failed for user \"postgres.x\"", LEAKY) == \
        "FATAL: password authentication failed for user \"postgres.x\""


def test_service_refuses_to_start_on_a_malformed_string_without_quoting_it():
    found = Config(store="postgres", database_url=LEAKY, auth_modes=("sheet_key",)).problems()
    assert any("DATABASE_URL: the password contains a %" in p for p in found)
    assert all(_clean(p) for p in found)


@pytest.mark.parametrize("script", ["apply_migrations.py", "verify_db.py"])
def test_scripts_report_a_malformed_string_cleanly(script):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / script)],
                            env=dict(os.environ, DATABASE_URL=LEAKY), capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert "isn't percent-encoded" in result.stderr
    assert _clean(result.stdout + result.stderr)
    assert "Traceback" not in result.stderr


def test_check_dsn_encodes_a_raw_password_in_place(tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_dsn", ROOT / "scripts" / "check_dsn.py")
    check_dsn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check_dsn)
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=x\nDATABASE_URL=" + LEAKY.replace(PASSWORD, "[" + PASSWORD + "]") + "\n")
    assert check_dsn.main(["--env", str(env)]) == 1
    assert check_dsn.main(["--env", str(env), "--encode-password"]) == 0
    printed = capsys.readouterr().out
    assert _clean(printed) and "PROBLEM" in printed and "RAN: DATABASE_URL is well formed" in printed
    text = env.read_text()
    assert text.startswith("OPENROUTER_API_KEY=x\n") and "pw%24Tz%259K%26secret@" in text and "[" not in text
    from urllib.parse import unquote, urlsplit
    assert unquote(urlsplit(text.split("DATABASE_URL=", 1)[1].strip()).password) == PASSWORD
