"""A fresh deployment has nothing to clean up.

Builds what the image holds from the Dockerfile's own COPY lines, applies the
migrations and the seed to an empty database, and starts that copy of the
service the way the README's Railway variables configure it: production,
Postgres under the least-privilege login, live Jev, Google sign-in and sheet
keys. Then it looks for everything a team would otherwise have to find and
remove:

  - people, calibrations or cached Jev answers in the database, before and
    after the service has started;
  - anything in the image beyond the service and its locked dependencies;
  - starter criteria that arrive calibrated or with someone's review dates;
  - demo routes on the service, demo items in the sheet's menu, and demo
    settings in the setup steps.

Every problem is collected before the test fails, so a run against older code
lists all of them. Live Jev is configured with a key that is not a key: no
check here asks Jev anything.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from conftest import ROOT

psycopg = pytest.importorskip("psycopg")
pytestmark = [pytest.mark.postgres, pytest.mark.e2e]

SNAPSHOT = "typesafe/jev-1.13-20260917"
JEV_CRITERIA = ("C1", "C2", "C3", "C4", "C5", "RESTATE")
SHEET = "fresh-deployment-sheet"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(args, env, cwd=None):
    return subprocess.run([sys.executable] + args, env=env, cwd=cwd, capture_output=True, text=True, timeout=300)


def _with_db(dsn: str, user, database: str) -> str:
    parts = urlsplit(dsn)
    netloc = parts.netloc if user is None else "{0}@{1}".format(user, parts.netloc.rsplit("@", 1)[-1])
    return urlunsplit((parts.scheme, netloc, "/" + database, parts.query, parts.fragment))


def image_tree(root: Path) -> Path:
    """The files the image would hold, laid out under root/app as the Dockerfile copies them."""
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text()
    workdir = re.search(r"^WORKDIR\s+(\S+)", dockerfile, re.MULTILINE).group(1)
    for source, dest in re.findall(r"^COPY\s+(\S+)\s+(\S+)", dockerfile, re.MULTILINE):
        target = Path(dest if dest.startswith("/") else str(Path(workdir) / dest))
        target = root / target.relative_to("/")
        origin = ROOT / source
        if origin.is_dir():
            shutil.copytree(origin, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    return root / Path(workdir).relative_to("/")


@pytest.fixture
def deployment(pg_dsn, tmp_path):
    suffix = uuid.uuid4().hex[:10]
    name, login = "fresh_" + suffix, "svc_" + suffix
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute("create database {0}".format(name))
    owner = _with_db(pg_dsn, None, name)
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "local_supabase_shim.sql").read_text())
    admin_env = dict(os.environ, DATABASE_URL=owner, COACH_ENV_FILE="none")
    applied = _run([str(ROOT / "scripts" / "apply_migrations.py"), "--seed"], admin_env)
    assert applied.returncode == 0, applied.stderr
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute("create role {0} login in role coach_service".format(login))

    app_root = tmp_path / "image"
    workdir = image_tree(app_root)
    # What the image holds as built, listed before anything runs from it.
    built = sorted(str(p.relative_to(app_root)) for p in app_root.rglob("*") if p.is_file())
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COACH_", "JEV_", "GOOGLE_", "OPENROUTER"))}
    env.update(COACH_ENV_FILE="none", COACH_ENV="production", COACH_STORE="postgres",
               DATABASE_URL=_with_db(pg_dsn, login, name), JEV_MODE="live", JEV_PROVIDER="openrouter",
               OPENROUTER_API_KEY="sk-or-fresh-deployment-test-not-a-key", JEV_ALLOWED_SNAPSHOTS=SNAPSHOT,
               COACH_AUTH_MODES="google,sheet_key", GOOGLE_AUDIENCES="fresh-deployment.apps.googleusercontent.com",
               COACH_RATE_LIMIT_PER_MINUTE="0", PYTHONDONTWRITEBYTECODE="1")  # as the Dockerfile's ENV
    imported = _run(["-c", "import coach; print(coach.__file__)"], env, cwd=str(workdir))
    assert imported.stdout.strip().startswith(str(workdir)), "the service must run from the image's files: " + \
        imported.stdout + imported.stderr

    port = _free_port()
    log_path = app_root / "server.log"
    log = open(log_path, "w")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "coach.api:app", "--host", "127.0.0.1",
                                "--port", str(port)], cwd=str(workdir), env=env, stdout=log, stderr=subprocess.STDOUT)
    url = "http://127.0.0.1:{0}".format(port)
    try:
        deadline = time.time() + 30
        while True:
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=2) as response:
                    if response.status == 200:
                        break
            except Exception:  # noqa: BLE001 - still starting
                if process.poll() is not None or time.time() > deadline:
                    log.close()
                    raise RuntimeError("service did not start:\n" + log_path.read_text()[-3000:])
                time.sleep(0.2)
        # The first request builds the app; one more makes sure startup has finished its work.
        urllib.request.urlopen(url + "/healthz", timeout=5).read()
        yield {"url": url, "owner": owner, "admin_env": admin_env, "files": built}
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
        with psycopg.connect(pg_dsn, autocommit=True) as admin:
            admin.execute("drop database if exists {0} with (force)".format(name))
            admin.execute("drop role if exists {0}".format(login))


def _call(url, method, path, key, body=None):
    request = urllib.request.Request(url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Coach-Sheet-Key": key,
                                              "X-Coach-User": "reviewer@example.com"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_a_fresh_deployment_has_nothing_to_clean_up(deployment):
    problems = []
    checks = 0

    # The database, after the service has started.
    with psycopg.connect(deployment["owner"]) as conn:
        for table, what in (("team_roles", "people on the team list"), ("calibrations", "calibrations"),
                            ("jev_responses", "cached Jev answers")):
            count = conn.execute("select count(*) from {0}".format(table)).fetchone()[0]
            checks += 1
            if count:
                problems.append("the database starts with {0} {1}".format(count, what))

    # The image: the service and its locked dependencies, nothing else.
    files = deployment["files"]
    assert "app/backend/coach/api.py" in files, "the image probe must read the image's files"
    extra = [f for f in files if not (f == "app/backend/requirements.lock" or
                                      (f.startswith("app/backend/coach/") and f.endswith(".py")) or
                                      f == "app/backend/coach/starter_criteria.csv")]
    checks += 1
    if extra:
        problems.append("the image holds {0} file(s) beyond the service, e.g. {1}".format(len(extra), extra[:3]))

    # The starter criteria, as a sheet's Set up sheet would load them.
    issued = _run([str(ROOT / "scripts" / "sheet_keys.py"), "issue", "--sheet", SHEET, "--role", "reviewer"],
                  deployment["admin_env"])
    assert issued.returncode == 0, issued.stderr
    key = re.search(r"^(fcc_k_\S+)$", issued.stdout, re.MULTILINE).group(1)
    status, defaults = _call(deployment["url"], "GET", "/v1/defaults", key)
    assert status == 200, defaults
    criteria = defaults["criteria"]
    for row in criteria:
        if row["ID"] in JEV_CRITERIA:
            checks += 1
            if row["Calibration status"] != "Uncalibrated":
                problems.append("starter {0} arrives {1}".format(row["ID"], row["Calibration status"]))
        checks += 1
        if row.get("Last reviewed"):
            problems.append("starter {0} arrives reviewed on {1}".format(row["ID"], row["Last reviewed"]))
    status, looked_up = _call(deployment["url"], "POST", "/v1/criteria/status", key,
                              {"sheet_id": SHEET, "criteria": criteria})
    assert status == 200, looked_up
    for item in looked_up["criteria"]:
        if item["id"] in JEV_CRITERIA:
            checks += 1
            if item["calibration_status"] != "Uncalibrated":
                problems.append("the service calls starter {0} {1}".format(item["id"], item["calibration_status"]))

    # No demo: routes, menu items, settings.
    for method, path, body in (("GET", "/v1/demo/closes/2026-09", None),
                               ("POST", "/v1/demo/history", {"sheet_id": SHEET + "/demo-1"})):
        status, answer = _call(deployment["url"], method, path, key, body)
        checks += 1
        if (status, answer.get("detail")) != (404, "Not Found"):
            problems.append("{0} {1} exists on the service ({2}: {3})".format(method, path, status,
                                                                             answer.get("detail")))
    script = (ROOT / "apps_script" / "Code.gs").read_text()
    menu = re.findall(r"\.addItem\('([^']+)'", script)
    checks += 1
    assert "Run Coach" in menu, "the menu probe must read real menu items"
    if [m for m in menu if "demo" in m.lower()] or "/v1/demo" in script:
        problems.append("the sheet's menu has demo items: {0}".format([m for m in menu if "demo" in m.lower()]))
    example = (ROOT / "backend" / "env.example").read_text()
    railway = re.findall(r"^\| `([A-Z_]+)` \|", (ROOT / "README.md").read_text(), re.MULTILINE)
    checks += 1
    assert "DATABASE_URL" in railway, "the setup probe must read the README's Railway variables"
    demo_settings = [v for v in railway + re.findall(r"^([A-Z_]+)=", example, re.MULTILINE) if "DEMO" in v]
    if demo_settings:
        problems.append("the setup steps name demo settings: {0}".format(sorted(set(demo_settings))))

    assert not problems, "a fresh deployment would need cleaning up:\n  - " + "\n  - ".join(problems)
    print("\nRAN: fresh deployment, {0} checks, nothing to clean up".format(checks))
