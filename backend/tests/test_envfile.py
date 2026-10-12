"""backend/.env: how it is parsed, which value wins, and that the test suite never reads the real one."""

from __future__ import annotations

import os

from coach import envfile
from conftest import ROOT


def test_env_file_parsing_and_precedence(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "export PROBE_A=one\n"
        "PROBE_B = 'two words'\n"
        'PROBE_C="three"\n'
        "PROBE_EMPTY=\n"
        "not a pair\n"
    )
    assert envfile.parse(path) == {"PROBE_A": "one", "PROBE_B": "two words", "PROBE_C": "three", "PROBE_EMPTY": ""}
    monkeypatch.setenv("PROBE_A", "from-environment")
    monkeypatch.delenv("PROBE_B", raising=False)
    monkeypatch.delenv("PROBE_EMPTY", raising=False)
    envfile.load(path)
    assert os.environ["PROBE_A"] == "from-environment"  # the environment wins
    assert os.environ["PROBE_B"] == "two words"
    assert "PROBE_EMPTY" not in os.environ
    assert envfile.parse(tmp_path / "missing") == {}


def test_the_suite_never_reads_the_real_env_file(tmp_path):
    """Scripts started by tests must not see backend/.env's credentials."""
    import subprocess
    import sys

    assert os.environ.get("COACH_ENV_FILE") == "none" and envfile.default_path() is None
    probe = ("import os, sys; sys.path.insert(0, 'backend'); from coach import envfile; "
             "os.environ.pop('PROBE_SECRET', None); envfile.load(); print(os.environ.get('PROBE_SECRET', 'unseen'))")
    fake = tmp_path / ".env"
    fake.write_text("PROBE_SECRET=seen\n")
    run = lambda env: subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), env=env,  # noqa: E731
                                     capture_output=True, text=True).stdout.strip()
    base = {k: v for k, v in os.environ.items() if k != "PROBE_SECRET"}
    assert run(base) == "unseen"
    assert run(dict(base, COACH_ENV_FILE=str(fake))) == "seen"  # the probe can see a file when pointed at one


def test_set_value_replaces_or_appends_and_keeps_the_file_private(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nB=2")
    envfile.set_value(path, "B", "two")
    envfile.set_value(path, "C", "three", comment="added by a script")
    assert path.read_text() == "A=1\nB=two\n\n# added by a script\nC=three\n"
    assert envfile.parse(path) == {"A": "1", "B": "two", "C": "three"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
