"""Recorded Jev answers: replay them offline, and cache them online.

A recording is one real answer to one question, keyed by the question's
fingerprint (request model + state + question). Recordings are per question
rather than per request because Jev evaluates each question in isolation
against the same state, and because it means switching one criterion off, or
editing one pass condition, misses only that criterion's recordings.

Replay is strict: a question with no recording raises JevReplayMiss. It never
falls back to a guess, a default or a nearby answer, because a replayed
answer that was not actually Jev's would look exactly like one that was.

The cache wraps a live client: answers already known for an identical
question are reused, so a rerun of unchanged commentary gets the same verdict
and costs nothing. Cached answers from a snapshot outside the allowlist are
ignored and re-asked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Protocol, Sequence

from .client import JevAnswer, JevReplayMiss, JevResponse, JevError, check_snapshot
from .questions import question_fingerprint


class Recording(NamedTuple):
    fingerprint: str
    question_id: str
    request_model: str
    model: str
    probability: float
    source: str  # live | synthetic
    text_id: str = ""
    input_tokens: Optional[int] = None


def load_recordings(path: Path) -> Dict[str, Recording]:
    entries: Dict[str, Recording] = {}
    if not path.exists():
        return entries
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        data = json.loads(raw)
        record = Recording(
            fingerprint=data["fingerprint"],
            question_id=data["question_id"],
            request_model=data["request_model"],
            model=data["model"],
            probability=float(data["probability"]),
            source=data.get("source", "live"),
            text_id=data.get("text_id", ""),
            input_tokens=data.get("input_tokens"),
        )
        if not 0.0 <= record.probability <= 1.0:
            raise ValueError("{0}:{1}: probability out of range".format(path, line_number))
        entries[record.fingerprint] = record
    return entries


def write_recordings(path: Path, records: Iterable[Recording]) -> None:
    ordered = sorted(records, key=lambda r: r.fingerprint)
    lines = [json.dumps(r._asdict(), sort_keys=True, separators=(",", ":")) for r in ordered]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


class ReplayJevClient:
    def __init__(self, recordings: Dict[str, Recording], request_model: str,
                 allowed_snapshots: Sequence[str] = ()) -> None:
        self.recordings = recordings
        self.model = request_model
        self.allowed = tuple(allowed_snapshots)
        self.calls = 0

    def evaluate(self, state: Dict[str, Any], questions: Dict[str, Any],
                 session_id: Optional[str] = None) -> JevResponse:
        self.calls += 1
        answers = {}
        missing = []
        for qid, question in questions.items():
            record = self.recordings.get(question_fingerprint(self.model, state, question))
            if record is None:
                missing.append(qid)
                continue
            served = check_snapshot(record.model, self.model, self.allowed)
            answers[qid] = JevAnswer(record.probability, served, "replay")
        if missing:
            raise JevReplayMiss(
                "no recorded Jev answer for {0}: replay mode only grades text that was recorded; "
                "run with JEV_MODE=live to ask Jev".format(", ".join(missing)))
        return JevResponse(answers, {"requests": 0}, 0)


class CachedAnswer(NamedTuple):
    probability: float
    model: str


class JevCache(Protocol):
    def get(self, fingerprint: str) -> Optional[CachedAnswer]: ...

    def put(self, fingerprint: str, question_id: str, answer: JevAnswer, usage: Dict[str, Any]) -> None: ...


class MemoryJevCache:
    def __init__(self, recordings: Optional[Dict[str, Recording]] = None) -> None:
        self._data: Dict[str, CachedAnswer] = {}
        for fp, record in (recordings or {}).items():
            self._data[fp] = CachedAnswer(record.probability, record.model)

    def get(self, fingerprint: str) -> Optional[CachedAnswer]:
        return self._data.get(fingerprint)

    def put(self, fingerprint: str, question_id: str, answer: JevAnswer, usage: Dict[str, Any]) -> None:
        self._data[fingerprint] = CachedAnswer(answer.probability, answer.model)

    def __len__(self) -> int:
        return len(self._data)


class CachingJevClient:
    def __init__(self, inner: Any, cache: JevCache, request_model: str,
                 allowed_snapshots: Sequence[str] = ()) -> None:
        self.inner = inner
        self.cache = cache
        self.model = request_model
        self.allowed = tuple(allowed_snapshots)

    def evaluate(self, state: Dict[str, Any], questions: Dict[str, Any],
                 session_id: Optional[str] = None) -> JevResponse:
        answers: Dict[str, JevAnswer] = {}
        misses: Dict[str, Any] = {}
        prints = {qid: question_fingerprint(self.model, state, q) for qid, q in questions.items()}
        for qid, question in questions.items():
            cached = self.cache.get(prints[qid])
            if cached is not None:
                try:
                    served = check_snapshot(cached.model, self.model, self.allowed)
                except JevError:
                    cached = None
                else:
                    answers[qid] = JevAnswer(cached.probability, served, "cache")
            if cached is None:
                misses[qid] = question
        usage: Dict[str, Any] = {"requests": 0}
        requests = 0
        if misses:
            fresh = self.inner.evaluate(state, misses, session_id)
            requests = fresh.requests
            usage = dict(fresh.usage)
            for qid, answer in fresh.answers.items():
                self.cache.put(prints[qid], qid, answer, usage)
                answers[qid] = answer
        return JevResponse(answers, usage, requests)


def recordings_from_response(state: Dict[str, Any], questions: Dict[str, Any], response: JevResponse,
                             request_model: str, text_id: str = "") -> List[Recording]:
    tokens = response.usage.get("input_tokens")
    return [
        Recording(
            fingerprint=question_fingerprint(request_model, state, questions[qid]),
            question_id=qid,
            request_model=request_model,
            model=answer.model,
            probability=round(answer.probability, 4),
            source="live",
            text_id=text_id,
            input_tokens=tokens if isinstance(tokens, int) else None,
        )
        for qid, answer in response.answers.items()
    ]
