"""Drive the real Code.gs against the real service over HTTP.

Starts the service under uvicorn (replay Jev on the recorded answers, memory
store seeded with Fernhill's team and calibrations, Google auth only), with
Google's JWKS replaced by a local one whose key signs two identity tokens: one
for the reviewer, one for the preparer.
Then runs apps_script/test/e2e.test.mjs under Node, which loads Code.gs
unmodified, pastes September into the Flux tab without Line IDs, and plays the
five steps through it with curl-backed UrlFetchApp. Passes only if Node exits
0 and printed its completion marker.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from jwt.algorithms import RSAAlgorithm

import fernhill
from conftest import BACKEND, ROOT
from helpers import AUDIENCE, Keys

NODE = shutil.which("node")
CURL = shutil.which("curl")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(NODE is None or CURL is None, reason="needs node and curl")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def keys():
    return Keys()


@pytest.fixture(scope="module")
def jwks_url(keys):
    jwk = json.loads(RSAAlgorithm.to_jwk(keys.public))
    jwk.update(kid="k1", use="sig", alg="RS256")
    body = json.dumps({"keys": [jwk]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:{0}/jwks".format(server.server_address[1])
    server.shutdown()


@pytest.fixture(scope="module")
def service(jwks_url):
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "COACH_ENV": "development", "COACH_STORE": "memory", "JEV_MODE": "replay",
        "JEV_ALLOWED_SNAPSHOTS": fernhill.SNAPSHOT, "JEV_RECORDINGS": str(fernhill.RECORDINGS),
        "COACH_AUTH_MODES": "google",
        "GOOGLE_AUDIENCES": AUDIENCE, "GOOGLE_JWKS_URL": jwks_url,
        "COACH_RATE_LIMIT_PER_MINUTE": "0",
    })
    log_path = ROOT / ".pytest_e2e_server.log"
    log = open(log_path, "w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "served_app:app", "--app-dir", str(BACKEND / "tests"),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=log, stderr=subprocess.STDOUT)
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
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
        log_path.unlink(missing_ok=True)


NUMBERS = ("Prior", "Current", "Variance ($)", "Variance (%)")


def _close_file(tmp_path) -> str:
    """September's rows as a team would paste them: typed numbers, no results yet."""
    rows = [{k: (float(v) if k in NUMBERS and v != "" else v) for k, v in r.items()}
            for r in fernhill.flux_rows(fernhill.CLOSE)]
    path = tmp_path / "september.json"
    path.write_text(json.dumps(rows))
    return str(path)


def test_five_steps_through_the_real_script(service, keys, tmp_path):
    revision = fernhill.versions(fernhill.CLOSE)[fernhill.HOSTING_LINE][1]
    env = dict(os.environ)
    env.update({
        "COACH_TEST_URL": service,
        "COACH_TEST_REVIEWER_TOKEN": keys.token("reviewer@example.com"),
        "COACH_TEST_PREPARER_TOKEN": keys.token("preparer@example.com"),
        "COACH_TEST_REVISION": revision,
        "COACH_TEST_CLOSE_FILE": _close_file(tmp_path),
    })
    result = subprocess.run([NODE, "--test", str(ROOT / "apps_script" / "test" / "e2e.test.mjs")],
                            env=env, capture_output=True, text=True, timeout=300)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-6000:]
    assert "RAN: e2e complete" in output, output[-3000:]
    for marker in ("RAN: e2e steps 1-3", "RAN: e2e steps 4-5, status: Passed (attempt 2)", "RAN: e2e mid-run edit"):
        assert marker in output
    print("\n".join(line for line in output.splitlines() if "RAN:" in line))
