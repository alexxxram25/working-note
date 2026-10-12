"""The deployed shape, locally, in the order a team sets it up.

Applies the migrations and seed to a fresh database and starts the service
with COACH_STORE=postgres (replay Jev, sheet-key auth), logged in as a role
that has only coach_service's grants (the least-privilege login the README
recommends; the admin scripts keep the owner's DSN). Then, with the admin
scripts: a reviewer key for one sheet, the team list, a first run on which
nothing is calibrated (C1's flag is a prompt for the reviewer), calibration
on the team's labels with scripts/calibrate.py --write-db, and the five steps
over HTTP. If this passes, the production configuration works end to end
except for Google sign-in, which test_apps_script_e2e.py covers.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

from conftest import BACKEND, ROOT
import fernhill
from five_steps import FiveSteps, Remote

psycopg = pytest.importorskip("psycopg")
pytestmark = [pytest.mark.postgres, pytest.mark.e2e]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(args, env, timeout=300):
    return subprocess.run([sys.executable] + args, env=env, capture_output=True, text=True, timeout=timeout)


def _as_user(dsn: str, user: str, database: str) -> str:
    parts = urlsplit(dsn)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, "{0}@{1}".format(user, host), "/" + database, parts.query, parts.fragment))


@pytest.fixture(scope="module")
def database():
    """(owner DSN, service DSN): the service's login holds coach_service's grants and nothing else."""
    base = os.environ.get("COACH_TEST_DSN")
    if not base:
        pytest.skip("COACH_TEST_DSN not set")
    suffix = uuid.uuid4().hex[:10]
    name, login = "coach_d_" + suffix, "svc_" + suffix
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute("create database {0}".format(name))
    parts = urlsplit(base)
    dsn = urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "local_supabase_shim.sql").read_text())
    env = dict(os.environ, DATABASE_URL=dsn)
    applied = _run([str(ROOT / "scripts" / "apply_migrations.py"), "--seed"], env)
    assert applied.returncode == 0, applied.stderr
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("create role {0} login in role coach_service".format(login))
    try:
        yield dsn, _as_user(base, login, name)
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute("drop database if exists {0} with (force)".format(name))
            admin.execute("drop role if exists {0}".format(login))


@pytest.fixture(scope="module")
def service(database):
    port = _free_port()
    env = dict(os.environ, COACH_ENV="production", COACH_STORE="postgres", DATABASE_URL=database[1],
               JEV_MODE="replay", JEV_ALLOWED_SNAPSHOTS=fernhill.SNAPSHOT, JEV_RECORDINGS=str(fernhill.RECORDINGS),
               COACH_AUTH_MODES="sheet_key", COACH_RATE_LIMIT_PER_MINUTE="0")
    log_path = ROOT / ".pytest_e2e_server.log"
    log = open(log_path, "w")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "coach.api:app", "--host", "127.0.0.1",
                                "--port", str(port)], cwd=str(BACKEND), env=env, stdout=log,
                               stderr=subprocess.STDOUT)
    url = "http://127.0.0.1:{0}".format(port)
    try:
        deadline = time.time() + 30
        while True:
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=2) as response:
                    if response.status == 200:
                        break
            except Exception:  # noqa: BLE001
                if process.poll() is not None or time.time() > deadline:
                    log.close()
                    raise RuntimeError("service did not start:\n" + log_path.read_text()[-3000:])
                time.sleep(0.2)
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
        log_path.unlink(missing_ok=True)


def test_production_shape_end_to_end(database, service, tmp_path):
    import json

    owner_dsn, service_dsn = database
    with psycopg.connect(service_dsn) as conn:
        assert conn.execute("select current_user").fetchone()[0].startswith("svc_")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("insert into team_roles (email, role) values ('x@example.com', 'reviewer')")

    with urllib.request.urlopen(service + "/healthz") as response:
        health = json.loads(response.read())
    assert health["store"] == "postgres" and health["env"] == "production" and health["store_ok"] is True

    env = dict(os.environ, DATABASE_URL=owner_dsn)
    sheet = "e2e-sheet-" + uuid.uuid4().hex[:6]
    wildcard = _run([str(ROOT / "scripts" / "sheet_keys.py"), "issue", "--sheet", "*", "--role", "reviewer"], env)
    assert wildcard.returncode != 0 and "one spreadsheet ID" in wildcard.stderr, wildcard.stdout + wildcard.stderr
    issued = _run([str(ROOT / "scripts" / "sheet_keys.py"), "issue", "--sheet", sheet, "--role", "reviewer",
                   "--label", "e2e", "--rate", "600"], env)
    assert issued.returncode == 0, issued.stderr
    key = re.search(r"^(fcc_k_\S+)$", issued.stdout, re.MULTILINE).group(1)

    # A key saved for a script goes to the env file, never to the terminal.
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED=keep\n")
    file_env = dict(env, COACH_ENV_FILE=str(env_file))
    saved = _run([str(ROOT / "scripts" / "sheet_keys.py"), "issue", "--sheet", sheet, "--role", "reviewer",
                  "--label", "e2e saved", "--save-as", "COACH_SHEET_KEY"], file_env)
    assert saved.returncode == 0, saved.stderr
    assert "fcc_k_" not in saved.stdout + saved.stderr and "as COACH_SHEET_KEY (not shown)" in saved.stdout
    assert env_file.read_text().startswith("UNRELATED=keep\n") and "COACH_SHEET_KEY=fcc_k_" in env_file.read_text()

    empty = _run([str(ROOT / "scripts" / "team.py"), "list"], env)
    assert "RAN: 0 team member(s)" in empty.stdout, "the seed adds nobody to the team"
    for email, role in (("reviewer@example.com", "reviewer"), ("Preparer@Example.com", "preparer")):
        team = _run([str(ROOT / "scripts" / "team.py"), "add", email, "--role", role], env)
        assert team.returncode == 0 and "{0} is a {1}".format(email.lower(), role) in team.stdout
    listed = _run([str(ROOT / "scripts" / "team.py"), "list"], env)
    assert "preparer@example.com" in listed.stdout and "RAN: 2 team member(s)" in listed.stdout

    # Nothing ships calibrated: on a fresh deployment C1's flag is a prompt for the reviewer.
    steps = FiveSteps(Remote(service, key), sheet)
    first = steps.run(fernhill.lines_for(fernhill.CLOSE))
    status = {c["id"]: c["calibration_status"] for c in first["criteria_status"]}
    assert {status[c] for c in fernhill.JEV_CRITERIA} == {"Uncalibrated"}, status
    line = next(r for r in first["results"] if r["line_id"] == fernhill.HOSTING_LINE)
    assert line["status"] != "needs_revision" and line["failed"] == [], line
    print("RAN: production shape, before calibration: {0}".format(line["status_label"]))

    # The team calibrates on its own labels, as the README's onboarding steps do.
    labels = fernhill.write_team_labels(tmp_path / "labels.csv")
    calibrated = _run([str(ROOT / "scripts" / "calibrate.py"), "--labels", str(labels),
                       "--criteria", str(fernhill.DIR / "criteria.csv"), "--recordings", str(fernhill.RECORDINGS),
                       "--out", str(tmp_path / "team_calibration.json"), "--write-db"], env)
    assert calibrated.returncode == 0, calibrated.stdout + calibrated.stderr
    assert "RAN: 6 calibration(s) recorded in the database" in calibrated.stdout

    steps.play()
    print("\n".join("RAN: production shape, " + line for line in steps.log))

    with psycopg.connect(service_dsn) as conn:
        rows = conn.execute("select count(*) from log").fetchone()[0]
        assert rows > 100, rows
        assert conn.execute("select count(*) from runs where status = 'completed'").fetchone()[0] == 3
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("delete from log")
    with psycopg.connect(owner_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("delete from log")

    listed_keys = _run([str(ROOT / "scripts" / "sheet_keys.py"), "list"], env)
    key_id = re.search(r"^(k_[0-9a-f]+)", listed_keys.stdout, re.MULTILINE).group(1)
    revoked = _run([str(ROOT / "scripts" / "sheet_keys.py"), "revoke", key_id], env)
    assert revoked.returncode == 0
    status, body = Remote(service, key).call("GET", "/v1/me", "reviewer")
    assert status == 401, (status, body)
