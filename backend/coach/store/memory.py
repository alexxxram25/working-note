"""An in-process Store: tests and local runs without a database.

Behaves like PostgresStore down to the append-only rule: there is no method
that updates or deletes a Log row, and the conformance suite checks both
stores give the same answers. Everything is lost when the process exits, which
is why production refuses it unless explicitly allowed.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..attempts import PriorAttempt
from ..criteria_versions import Change
from ..hashing import commentary_hash, sha256_hex
from ..jev.client import JevAnswer
from ..jev.replay import CachedAnswer
from ..models import LineIn
from .base import LOG_FIELDS, LockState, SheetKey, Store, TeamMember, utcnow


class MemoryStore(Store):
    kind = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._team: Dict[str, TeamMember] = {}
        self._keys: Dict[str, SheetKey] = {}
        self._settings: Dict[str, Any] = {}
        self._default_criteria: List[Dict[str, Any]] = []
        self._calibrations: List[Tuple[str, str, str]] = []
        self._snapshots: Dict[str, List[Tuple[int, str]]] = {}
        self._history: List[Dict[str, Any]] = []
        self._locks: Dict[str, Tuple[str, Optional[str], dt.datetime]] = {}
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._lines: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._commentary: Dict[Tuple[str, str, str], str] = {}
        self._log: List[Dict[str, Any]] = []
        self._overrides: List[Dict[str, Any]] = []
        self._override_keys: set = set()
        self._jev: Dict[str, CachedAnswer] = {}
        self._health: List[Dict[str, Any]] = []
        self._next_id = 1

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    # team and keys ---------------------------------------------------------
    def team_member(self, email: str) -> Optional[TeamMember]:
        with self._lock:
            return self._team.get((email or "").lower())

    def upsert_team_member(self, email: str, role: str, display_name: Optional[str] = None,
                           active: bool = True) -> None:
        if role not in ("preparer", "reviewer"):
            raise ValueError("role must be preparer or reviewer")
        with self._lock:
            self._team[email.lower()] = TeamMember(email.lower(), role, display_name, active)

    def team_members(self) -> List[TeamMember]:
        with self._lock:
            return sorted(self._team.values(), key=lambda t: t.email)

    def sheet_key(self, key_hash: str) -> Optional[SheetKey]:
        with self._lock:
            return next((k for k in self._keys.values() if k.key_hash == key_hash), None)

    def add_sheet_key(self, key: SheetKey, created_by: Optional[str] = None) -> None:
        with self._lock:
            if key.key_id in self._keys:
                raise ValueError("key id exists")
            self._keys[key.key_id] = key

    def revoke_sheet_key(self, key_id: str) -> bool:
        with self._lock:
            key = self._keys.get(key_id)
            if key is None or key.revoked_at is not None:
                return False
            self._keys[key_id] = key._replace(revoked_at=utcnow())
            return True

    def touch_sheet_key(self, key_id: str) -> None:
        return None

    # defaults --------------------------------------------------------------
    def set_defaults(self, settings: Dict[str, Any], criteria: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._settings = dict(settings)
            self._default_criteria = list(criteria)

    def default_settings(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def default_criteria(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(c) for c in self._default_criteria]

    # calibration -----------------------------------------------------------
    def calibration_records(self) -> List[Tuple[str, str, str]]:
        with self._lock:
            return list(self._calibrations)

    def add_calibration(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self._calibrations.append((record["criterion_id"], record["fingerprint"], record["status"].lower()))

    # criteria versions -----------------------------------------------------
    def criteria_state(self, sheet_id: str) -> Tuple[Optional[str], Dict[str, Tuple[int, Dict[str, str]]]]:
        with self._lock:
            snapshots = self._snapshots.get(sheet_id) or []
            latest = snapshots[-1][1] if snapshots else None
            state: Dict[str, Tuple[int, Dict[str, str]]] = {}
            for row in self._history:
                if row["sheet_id"] == sheet_id:
                    current = state.get(row["criterion_id"])
                    if current is None or row["version"] > current[0]:
                        state[row["criterion_id"]] = (row["version"], row["content"])
            return latest, state

    def record_criteria(self, sheet_id, snapshot_hash, content, changes: Sequence[Change], changed_by,
                        detected_by, run_id) -> Optional[int]:
        with self._lock:
            snapshots = self._snapshots.setdefault(sheet_id, [])
            if snapshots and snapshots[-1][1] == snapshot_hash and not changes:
                return snapshots[-1][0]
            snapshot_id = self._id()
            snapshots.append((snapshot_id, snapshot_hash))
            now = utcnow()
            for change in changes:
                if any(r["sheet_id"] == sheet_id and r["criterion_id"] == change.criterion_id
                       and r["version"] == change.version for r in self._history):
                    raise ValueError("criteria version already recorded")
                self._history.append({
                    "sheet_id": sheet_id, "criterion_id": change.criterion_id, "version": change.version,
                    "content": dict(change.content), "content_hash": change.content_hash,
                    "fields_changed": list(change.fields_changed), "diff": dict(change.diff),
                    "calibration_impact": change.calibration_impact,
                    "changed_by": changed_by.get(change.criterion_id), "detected_by": detected_by,
                    "detected_in_run": run_id, "snapshot_id": snapshot_id, "created_at": now,
                })
            return snapshot_id

    def criteria_history(self, sheet_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [dict(r) for r in self._history if r["sheet_id"] == sheet_id]
        rows.sort(key=lambda r: (r["created_at"], r["criterion_id"], r["version"]))
        return rows[-limit:]

    # runs and the lease ----------------------------------------------------
    def acquire_lock(self, sheet_id: str, run_id: str, holder: Optional[str], ttl_seconds: int) -> LockState:
        with self._lock:
            now = utcnow()
            current = self._locks.get(sheet_id)
            if current is None or current[2] < now:
                expires = now + dt.timedelta(seconds=ttl_seconds)
                self._locks[sheet_id] = (run_id, holder, expires)
                return LockState(True, run_id, holder, expires)
            return LockState(False, current[0], current[1], current[2])

    def release_lock(self, sheet_id: str, run_id: str) -> bool:
        with self._lock:
            current = self._locks.get(sheet_id)
            if current is not None and current[0] == run_id:
                del self._locks[sheet_id]
                return True
            return False

    def create_run(self, run: Dict[str, Any]) -> None:
        with self._lock:
            record = dict(run)
            record.setdefault("status", "running")
            record.setdefault("started_at", utcnow())
            record.setdefault("stale_line_ids", [])
            record.setdefault("lines_received", 0)
            record.setdefault("lines_evaluated", 0)
            record.setdefault("jev_requests", 0)
            self._runs[str(run["run_id"])] = record

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            run = self._runs.get(str(run_id))
            return dict(run) if run else None

    def finish_run(self, run_id, status, stale_line_ids=(), written_count=None, error=None) -> None:
        with self._lock:
            run = self._runs[str(run_id)]
            run.update(status=status, finished_at=utcnow(), stale_line_ids=list(stale_line_ids),
                       written_count=written_count, error=error)

    def record_evaluation(self, sheet_id, run_id, lines: Sequence[LineIn], log_rows, counts) -> None:
        with self._lock:
            now = utcnow()
            for line in lines:
                key = (sheet_id, line.line_id)
                existing = self._lines.get(key)
                if existing is None:
                    self._lines[key] = {"account": line.account, "first_seen_run": run_id, "first_seen_at": now}
                text = line.commentary or ""
                if text.strip():
                    self._commentary.setdefault((sheet_id, line.line_id, commentary_hash(text)), text)
            for row in log_rows:
                entry = {field: row.get(field) for field in LOG_FIELDS}
                entry.update(id=self._id(), ts=now, sheet_id=sheet_id, run_id=str(run_id))
                self._log.append(entry)
            run = self._runs.get(str(run_id))
            if run is not None:
                run.update({k: v for k, v in counts.items()})

    # reads -----------------------------------------------------------------
    def _stale(self) -> Dict[str, set]:
        return {rid: set(run.get("stale_line_ids") or []) for rid, run in self._runs.items()}

    def prior_attempts(self, sheet_id: str, line_ids: Iterable[str]) -> Dict[str, PriorAttempt]:
        wanted = set(line_ids)
        with self._lock:
            stale = self._stale()
            last_run: Dict[str, Tuple[int, str]] = {}
            for row in self._log:
                lid = row["line_id"]
                if (row["sheet_id"] != sheet_id or lid not in wanted or row["attempt_number"] < 1
                        or lid in stale.get(row["run_id"], set())):
                    continue
                if lid not in last_run or row["id"] > last_run[lid][0]:
                    last_run[lid] = (row["id"], row["run_id"])
            out: Dict[str, PriorAttempt] = {}
            for lid, (_, run_id) in last_run.items():
                rows = [r for r in self._log if r["sheet_id"] == sheet_id and r["line_id"] == lid
                        and r["run_id"] == run_id and r["attempt_number"] >= 1]
                out[lid] = PriorAttempt(
                    attempt_number=max(r["attempt_number"] for r in rows),
                    commentary_hash=rows[-1]["commentary_hash"],
                    failed=any(r["result"] == "fail" for r in rows),
                )
            return out

    def log_rows(self, sheet_id, close_periods=None, limit=None) -> List[Dict[str, Any]]:
        periods = set(close_periods) if close_periods else None
        with self._lock:
            rows = [dict(r) for r in self._log
                    if r["sheet_id"] == sheet_id and (periods is None or r["close_period"] in periods)]
        rows.sort(key=lambda r: r["id"])
        return rows[-limit:] if limit else rows

    def close_periods(self, sheet_id: str) -> List[str]:
        with self._lock:
            return sorted({r["close_period"] for r in self._log if r["sheet_id"] == sheet_id})

    # overrides -------------------------------------------------------------
    def add_overrides(self, rows: Sequence[Dict[str, Any]]) -> int:
        inserted = 0
        with self._lock:
            for row in rows:
                key = (row["sheet_id"], row["line_id"], row["kind"], row.get("criterion_id"),
                       bool(row.get("not_covered")), sha256_hex(row.get("note") or ""), row.get("submitted_by"))
                if key in self._override_keys:
                    continue
                self._override_keys.add(key)
                entry = dict(row)
                entry.update(id=self._id(), created_at=utcnow())
                self._overrides.append(entry)
                inserted += 1
        return inserted

    def overrides(self, sheet_id, close_periods=None) -> List[Dict[str, Any]]:
        periods = set(close_periods) if close_periods else None
        with self._lock:
            return [dict(r) for r in self._overrides
                    if r["sheet_id"] == sheet_id and (periods is None or r["close_period"] in periods)]

    # Jev cache -------------------------------------------------------------
    def jev_cache_get(self, fingerprint: str) -> Optional[CachedAnswer]:
        with self._lock:
            return self._jev.get(fingerprint)

    def jev_cache_put(self, fingerprint, question_id, answer: JevAnswer, usage, source="live") -> None:
        with self._lock:
            self._jev.setdefault(fingerprint, CachedAnswer(answer.probability, answer.model))

    # health ----------------------------------------------------------------
    def save_health(self, sheet_id, health_run_id, rows, generated_by) -> None:
        with self._lock:
            now = utcnow()
            for row in rows:
                entry = dict(row)
                entry.update(id=self._id(), sheet_id=sheet_id, health_run_id=health_run_id,
                             generated_at=now, generated_by=generated_by, resolution=None,
                             resolved_by=None, resolved_at=None)
                self._health.append(entry)

    def resolve_health(self, sheet_id, resolutions, resolved_by) -> int:
        updated = 0
        with self._lock:
            for item in resolutions:
                for row in self._health:
                    if row["sheet_id"] == sheet_id and row["id"] == item["id"]:
                        row.update(resolution=item["resolution"], resolved_by=resolved_by, resolved_at=utcnow())
                        updated += 1
        return updated

    def health_rows(self, sheet_id, health_run_id=None) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [dict(r) for r in self._health if r["sheet_id"] == sheet_id]
        if health_run_id is None and rows:
            health_run_id = rows[-1]["health_run_id"]
        return [r for r in rows if r["health_run_id"] == health_run_id]
