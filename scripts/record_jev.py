#!/usr/bin/env python3
"""Record real Jev answers for every text in a team's labeled sample.

    python scripts/record_jev.py --probe            # one real call on a sample line; prints the served snapshot

    # The team's labeled sample from onboarding, with its own criteria:
    python scripts/record_jev.py --labels labels.csv --criteria criteria.csv \
        --recordings team_recordings.jsonl --dry-run      # what would be sent, and what it costs
    python scripts/record_jev.py ... --yes                # record everything not yet recorded
    python scripts/record_jev.py ... --prune              # drop recordings no labeled text uses

Reads OPENROUTER_API_KEY (or TYPESAFE_API_KEY with --provider typesafe) from
the environment or backend/.env, and never prints it. Sends each labeled
text's row figures and commentary, with one noul question per active Jev
criterion, and appends the answers to the recordings file. Already-recorded
questions are skipped, so a rerun after editing a few texts only pays for
those.

The recordings are what calibrate.py measures thresholds on. Every one is a
real Jev answer; nothing is generated.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from coach import envfile, labeled_set  # noqa: E402
from coach.checks.materiality import variance_of  # noqa: E402
from coach.jev.client import JevError, LiveJevClient, validate_request_model  # noqa: E402
from coach.jev.questions import build_questions, build_state, question_fingerprint  # noqa: E402
from coach.jev.replay import load_recordings, recordings_from_response, write_recordings  # noqa: E402
from coach.models import Criterion, LineIn  # noqa: E402
from coach.seed import STARTER_CRITERIA  # noqa: E402

PRICE_PER_MILLION_INPUT = 0.042  # USD, published for jev-1.13; output tokens are free

# --probe asks about this line against the starter criteria: an invented row, no team data.
PROBE_LINE = LineIn(line_id="PROBE|2026-09|MoM|5000", entity="PROBE", period="2026-09", basis="MoM",
                    account="5000 Cloud hosting", prior="100000", current="148500",
                    commentary="Hosting rose $48.5K because a customer's one-time data load ran on our cloud account.")


def jev_criteria(path: Path) -> List[Criterion]:
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [c for c in (Criterion.from_sheet_row(r) for r in rows) if c.kind == "jev" and c.active]


def labeled_requests(labels_path, criteria) -> List[Tuple[str, Dict, Dict]]:
    questions = build_questions(criteria)
    out = []
    for item in labeled_set.load(labels_path, [c.id for c in criteria]):
        variance = variance_of(item.line.prior, item.line.current, item.line.variance_usd, item.line.variance_pct)
        out.append((item.text_id, build_state(item.line, variance.usd, variance.pct), questions))
    return out


def missing_only(requests, recorded, request_model):
    todo = []
    for text_id, state, questions in requests:
        missing = {qid: q for qid, q in questions.items()
                   if question_fingerprint(request_model, state, q) not in recorded}
        if missing:
            todo.append((text_id, state, missing))
    return todo


def make_client(provider: str, model: str) -> LiveJevClient:
    key_name = "OPENROUTER_API_KEY" if provider == "openrouter" else "TYPESAFE_API_KEY"
    key = os.environ.get(key_name, "").strip()
    if not key:
        raise SystemExit("{0} is not set (add it to backend/.env with: open -e backend/.env)".format(key_name))
    return LiveJevClient(provider, key, model, allowed_snapshots=(),
                         data_collection=os.environ.get("JEV_DATA_COLLECTION", "deny"))


def probe(provider: str, model: str, criteria: List[Criterion]) -> int:
    """One real call, to check the key and see which snapshot answers."""
    client = make_client(provider, model)
    variance = variance_of(PROBE_LINE.prior, PROBE_LINE.current, PROBE_LINE.variance_usd, PROBE_LINE.variance_pct)
    state, questions = build_state(PROBE_LINE, variance.usd, variance.pct), build_questions(criteria)
    try:
        response = client.evaluate(state, questions, session_id="working-note-probe")
    except JevError as exc:
        print("PROBE FAILED: {0}".format(exc))
        return 1
    snapshot = next(iter(response.answers.values())).model
    print("served snapshot: {0}".format(snapshot))
    for qid, answer in response.answers.items():
        print("  {0}: noul={1:.2f}".format(qid, answer.probability))
    print("usage: {0}".format({k: v for k, v in response.usage.items() if k != "requests"}))
    if provider == "openrouter" and snapshot != model:
        try:
            again = make_client(provider, snapshot).evaluate(state, questions, session_id="working-note-probe")
            print("dated snapshot as request model: accepted, served {0}".format(
                next(iter(again.answers.values())).model))
        except Exception as exc:  # noqa: BLE001
            print("dated snapshot as request model: refused ({0}); keep {1}".format(type(exc).__name__, model))
    print("RAN: probe complete")
    return 0


def main() -> int:
    envfile.load()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", default=os.environ.get("JEV_PROVIDER", "openrouter"))
    parser.add_argument("--model", default=os.environ.get("JEV_MODEL", "typesafe/jev-1.13-20260917"))
    parser.add_argument("--labels", type=Path, help="the team's labeled CSV from onboarding")
    parser.add_argument("--criteria", type=Path, help="the team's criteria.csv (--probe uses the starter criteria)")
    parser.add_argument("--recordings", type=Path, help="the recordings file to add to")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe", action="store_true", help="one real request on a sample line")
    parser.add_argument("--yes", action="store_true", help="actually record (costs money, fractions of a cent)")
    parser.add_argument("--prune", action="store_true",
                        help="drop recordings no current labeled text asks for (after editing texts)")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    model = validate_request_model(args.provider, args.model)
    if args.probe:
        return probe(args.provider, model, jev_criteria(args.criteria or STARTER_CRITERIA))
    if not (args.labels and args.criteria and args.recordings):
        parser.error("--labels, --criteria and --recordings are required (or use --probe)")
    criteria = jev_criteria(args.criteria)
    recorded = load_recordings(args.recordings)
    requests = labeled_requests(args.labels, criteria)
    todo = missing_only(requests, recorded, model)
    chars = sum(len(str(state)) + sum(len(str(q)) for q in qs.values()) for _, state, qs in todo)
    tokens = chars // 4
    print("provider={0} model={1} criteria={2}".format(args.provider, model, ",".join(c.id for c in criteria)))
    print("labeled texts={0}, already recorded={1} questions, requests to send={2}, questions to send={3}".format(
        len(requests), len(recorded), len(todo), sum(len(q) for _, _, q in todo)))
    print("estimated input tokens ~{0:,} -> ~${1:.4f} at ${2}/M input (output is free)".format(
        tokens, tokens / 1e6 * PRICE_PER_MILLION_INPUT, PRICE_PER_MILLION_INPUT))

    if args.dry_run:
        print("RAN: dry run only; nothing was sent")
        return 0

    if args.prune:
        wanted = {question_fingerprint(model, state, q) for _, state, qs in requests for q in qs.values()}
        kept = {fp: r for fp, r in recorded.items() if fp in wanted or r.request_model != model}
        write_recordings(args.recordings, kept.values())
        print("RAN: pruned {0} recording(s) no labeled text uses; {1} kept".format(len(recorded) - len(kept), len(kept)))
        return 0

    if not args.yes:
        print("Nothing sent. Re-run with --yes to record, or --probe for a single call.")
        return 2
    if not todo:
        print("RAN: every labeled text is already recorded")
        return 0

    client = make_client(args.provider, model)
    records = dict(recorded)
    failures = []
    cost = 0.0
    sent = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(client.evaluate, state, qs, "working-note-recording"): (text_id, state, qs)
                   for text_id, state, qs in todo}
        for future in as_completed(futures):
            text_id, state, qs = futures[future]
            try:
                response = future.result()
            except JevError as exc:
                failures.append((text_id, str(exc)))
                continue
            sent += response.requests
            cost += float(response.usage.get("cost") or 0.0)
            for record in recordings_from_response(state, qs, response, model, text_id):
                records[record.fingerprint] = record
    write_recordings(args.recordings, records.values())
    snapshots = sorted({r.model for r in records.values()})
    print("recorded {0} questions total; HTTP requests this run={1}; reported cost=${2:.5f}".format(
        len(records), sent, cost))
    print("served snapshots in recordings: {0}".format(", ".join(snapshots)))
    if failures:
        for text_id, message in failures[:10]:
            print("  FAILED {0}: {1}".format(text_id, message))
        print("{0} request(s) failed; rerun to retry only those".format(len(failures)))
        return 1
    print("RAN: recordings complete ({0})".format(args.recordings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
