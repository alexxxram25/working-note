#!/usr/bin/env python3
"""Push the Apps Script to one sheet, with that deployment's values filled in.

    python scripts/push_apps_script.py --script-id <Script ID> --service-url https://<service>
    python scripts/push_apps_script.py --dry-run ...     # stage the files and show what would be pushed

apps_script/.clasp.json and apps_script/appsscript.json in the repo are
templates: a placeholder Script ID and a placeholder service address. This
stages a copy of Code.gs and the manifest in a temporary folder, writes the
sheet's Script ID into .clasp.json and the service's address into the
manifest's urlFetchWhitelist (the only address the script may call), and runs
`clasp push` from there, so the repo never carries one deployment's values.

Needs `npx @google/clasp login` first (clasp is pinned below) and the Apps
Script API turned on at https://script.google.com/home/usersettings.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps_script"
CLASP = "@google/clasp@3.4.1"
SCRIPT_ID = re.compile(r"^[A-Za-z0-9_-]{30,80}$")
SERVICE_URL = re.compile(r"^https://[a-z0-9.-]+(?::\d+)?$")


def stage(folder: Path, script_id: str, service_url: str) -> Dict[str, str]:
    """Write Code.gs, the manifest and .clasp.json into folder; returns what was written."""
    if not SCRIPT_ID.match(script_id):
        raise ValueError("that doesn't look like a Script ID (Apps Script > Project Settings > Script ID)")
    service_url = service_url.rstrip("/")
    if not SERVICE_URL.match(service_url):
        raise ValueError("the service URL must be https://host with no path, like https://x.up.railway.app")
    manifest = json.loads((SOURCE / "appsscript.json").read_text())
    manifest["urlFetchWhitelist"] = [service_url + "/"]
    clasp = {"scriptId": script_id, "rootDir": "."}
    shutil.copyfile(SOURCE / "Code.gs", folder / "Code.gs")
    (folder / "appsscript.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (folder / ".clasp.json").write_text(json.dumps(clasp, indent=2) + "\n")
    (folder / ".claspignore").write_text("**/**\n!Code.gs\n!appsscript.json\n")
    return {"Code.gs": (folder / "Code.gs").read_text(), "appsscript.json": (folder / "appsscript.json").read_text(),
            ".clasp.json": (folder / ".clasp.json").read_text()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script-id", required=True)
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="working-note-gas-") as tmp:
        folder = Path(tmp)
        try:
            written = stage(folder, args.script_id, args.service_url)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        whitelist = json.loads(written["appsscript.json"])["urlFetchWhitelist"]
        print("staged Code.gs ({0} lines) and the manifest; urlFetchWhitelist = {1}".format(
            written["Code.gs"].count("\n"), whitelist))
        if args.dry_run:
            print("RAN: dry run; nothing pushed")
            return 0
        result = subprocess.run(["npx", "--yes", CLASP, "push", "--force"], cwd=str(folder))
        if result.returncode != 0:
            print("clasp push failed. Is clasp signed in (npx {0} login) and the Apps Script API on?".format(CLASP),
                  file=sys.stderr)
            return result.returncode
    print("RAN: pushed to script {0}; reload the sheet to see the Coach menu".format(args.script_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
