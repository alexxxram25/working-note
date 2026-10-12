"""Read KEY=VALUE lines from an env file without echoing anything.

Values already present in the environment win, so Railway variables are never
overridden by a stray file. Secrets read here are never printed or logged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional


def parse(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


DEFAULT = Path(__file__).resolve().parents[1] / ".env"


def default_path() -> Optional[Path]:
    """backend/.env, unless COACH_ENV_FILE names another file, or "none" for no file at all.

    The test suite sets COACH_ENV_FILE=none, so nothing it runs, scripts in
    subprocesses included, can pick up the real credentials in backend/.env.
    """
    override = os.environ.get("COACH_ENV_FILE", "").strip()
    if override.lower() == "none":
        return None
    return Path(override) if override else DEFAULT


def load(path: Optional[Path] = None) -> None:
    path = path or default_path()
    if path is None:
        return
    for key, value in parse(path).items():
        if value and not os.environ.get(key):
            os.environ[key] = value


def set_value(path: Path, key: str, value: str, comment: Optional[str] = None) -> None:
    """Set KEY=value in an env file: replace the existing line, or append it (after a comment)."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = "{0}={1}".format(key, value)
    pattern = re.compile(r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(lambda _: line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        block = "".join("# {0}\n".format(c) for c in (comment or "").splitlines() if c.strip())
        text += ("\n" if text else "") + block + line + "\n"
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
