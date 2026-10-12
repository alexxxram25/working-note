"""The Store on Postgres (Supabase in production).

Connects per operation: Supabase puts a transaction-mode pooler (port 6543) in
front of the database for exactly this shape of client. prepare_threshold=None
turns off psycopg's automatic prepared statements, which that pooler breaks:
each transaction may land on a different backend, so a statement prepared on
one is missing on the next. Left on, it passes every test and fails once
traffic warms up.

A run's writes (lines, commentary versions, Log rows, run counts) go in one
transaction, so a Log never holds half a run. The Log is insert-only in the
database itself (db/migrations/0002); nothing here tries otherwise.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..attempts import PriorAttempt
from ..criteria_versions import Change
from ..dsn import connect
from ..hashing import commentary_hash, sha256_hex
from ..jev.client import JevAnswer
from ..jev.replay import CachedAnswer
from ..models import LineIn
from .base import LOG_FIELDS, LeaseLost, LockState, SheetKey, Store, TeamMember, utcnow

try:  # pragma: no cover - import guard
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except Exception:  # noqa: BLE001
    psycopg = None


# db/migrations/0005: cached probabilities before it were rounded to four
# decimals. Those rows stay (the table is append-only) as generation 1; the
# service reads and writes generation 2 only.
JEV_CACHE_GENERATION = 2


class PostgresStore(Store):
    kind = "postgres"

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        if not dsn:
            raise ValueError("PostgresStore needs a connection string")
        self.dsn = dsn

    def _connect(self):
        return connect(self.dsn, prepare_threshold=None, row_factory=dict_row)

    def ping(self) -> bool:
        with self._connect() as conn:
            return conn.execute("select 1 as ok").fetchone()["ok"] == 1

    # team and keys ---------------------------------------------------------
    def team_member(self, email: str) -> Optional[TeamMember]:
        with self._connect() as conn:
            row = conn.execute("select email, role, display_name, active from team_roles where email = %s",
                               ((email or "").lower(),)).fetchone()
        return TeamMember(**row) if row else None

    def upsert_team_member(self, email, role, display_name=None, active=True) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into team_roles (email, role, display_name, active) values (%s, %s, %s, %s)"
                " on conflict (email) do update set role = excluded.role, display_name = excluded.display_name,"
                " active = excluded.active, updated_at = now()",
                (email.lower(), role, display_name, active))

    def team_members(self) -> List[TeamMember]:
        with self._connect() as conn:
            rows = conn.execute("select email, role, display_name, active from team_roles order by email").fetchall()
        return [TeamMember(**r) for r in rows]

    def sheet_key(self, key_hash: str) -> Optional[SheetKey]:
        with self._connect() as conn:
            row = conn.execute(
                "select key_id, key_hash, sheet_id, role_ceiling, label, rate_limit_per_minute, revoked_at"
                " from sheet_keys where key_hash = %s", (key_hash,)).fetchone()
        return SheetKey(**row) if row else None

    def add_sheet_key(self, key: SheetKey, created_by=None) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into sheet_keys (key_id, key_hash, sheet_id, role_ceiling, label, rate_limit_per_minute,"
                " created_by) values (%s, %s, %s, %s, %s, %s, %s)",
                (key.key_id, key.key_hash, key.sheet_id, key.role_ceiling, key.label, key.rate_limit_per_minute,
                 created_by))

    def revoke_sheet_key(self, key_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("update sheet_keys set revoked_at = now() where key_id = %s and revoked_at is null",
                                (key_id,)).rowcount == 1

    def touch_sheet_key(self, key_id: str) -> None:
        with self._connect() as conn:
            conn.execute("update sheet_keys set last_used_at = now() where key_id = %s", (key_id,))

    # defaults --------------------------------------------------------------
    def default_settings(self) -> Dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("select key, value from settings where sheet_id = '*'").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def default_criteria(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("select criteria from criteria_snapshots where sheet_id = '*'"
                               " order by id desc limit 1").fetchone()
        return list(row["criteria"]) if row else []

    # calibration -----------------------------------------------------------
    def calibration_records(self) -> List[Tuple[str, str, str]]:
        with self._connect() as conn:
            rows = conn.execute("select criterion_id, fingerprint, status from calibrations"
                                " order by created_at, id").fetchall()
        return [(r["criterion_id"], r["fingerprint"], r["status"]) for r in rows]

    def add_calibration(self, record: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into calibrations (criterion_id, fingerprint, model_snapshot, question_builder_version,"
                " passes_when_hash, threshold, status, metrics, labeled_set_hash, label_source, created_by)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (record["criterion_id"], record["fingerprint"], record["model_snapshot"],
                 record["question_builder_version"], record["passes_when_hash"], record["threshold"],
                 record["status"].lower(), Jsonb(record.get("metrics", {})), record["labeled_set_hash"],
                 record.get("label_source", "team"), record.get("created_by")))

    # criteria versions -----------------------------------------------------
    def criteria_state(self, sheet_id):
        with self._connect() as conn:
            # criteria_current, not the highest snapshot id: snapshots are deduplicated
            # by content, so a tab that returns to an earlier state has no new row.
            snap = conn.execute("select content_hash from criteria_current where sheet_id = %s",
                                (sheet_id,)).fetchone()
            rows = conn.execute(
                "select distinct on (criterion_id) criterion_id, version, content from criteria_history"
                " where sheet_id = %s order by criterion_id, version desc", (sheet_id,)).fetchall()
        return (snap["content_hash"] if snap else None,
                {r["criterion_id"]: (r["version"], r["content"]) for r in rows})

    def record_criteria(self, sheet_id, snapshot_hash, content, changes: Sequence[Change], changed_by,
                        detected_by, run_id) -> Optional[int]:
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    "insert into criteria_snapshots (sheet_id, content_hash, criteria, captured_by, captured_in_run)"
                    " values (%s, %s, %s, %s, %s) on conflict (sheet_id, content_hash) do nothing returning id",
                    (sheet_id, snapshot_hash, Jsonb(content), detected_by, run_id)).fetchone()
                if row is None:
                    row = conn.execute("select id from criteria_snapshots where sheet_id = %s and content_hash = %s",
                                       (sheet_id, snapshot_hash)).fetchone()
                snapshot_id = row["id"]
                conn.execute(
                    "insert into criteria_current (sheet_id, snapshot_id, content_hash, updated_in_run)"
                    " values (%s, %s, %s, %s) on conflict (sheet_id) do update set snapshot_id = excluded.snapshot_id,"
                    " content_hash = excluded.content_hash, updated_at = now(), updated_in_run = excluded.updated_in_run",
                    (sheet_id, snapshot_id, snapshot_hash, run_id))
                for change in changes:
                    conn.execute(
                        "insert into criteria_history (sheet_id, criterion_id, version, content, content_hash,"
                        " fields_changed, diff, calibration_impact, changed_by, detected_by, detected_in_run,"
                        " snapshot_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (sheet_id, change.criterion_id, change.version, Jsonb(change.content), change.content_hash,
                         list(change.fields_changed), Jsonb(change.diff), change.calibration_impact,
                         changed_by.get(change.criterion_id), detected_by, run_id, snapshot_id))
        return snapshot_id

    def criteria_history(self, sheet_id, limit=500):
        with self._connect() as conn:
            rows = conn.execute(
                "select * from (select criterion_id, version, content, content_hash, fields_changed, diff,"
                " calibration_impact, changed_by, detected_by, detected_in_run::text as detected_in_run,"
                " created_at from criteria_history where sheet_id = %s order by created_at desc, id desc"
                " limit %s) h order by created_at, criterion_id, version", (sheet_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # runs and the lease ----------------------------------------------------
    def acquire_lock(self, sheet_id, run_id, holder, ttl_seconds) -> LockState:
        with self._connect() as conn:
            row = conn.execute(
                "insert into run_locks (sheet_id, run_id, holder, expires_at)"
                " values (%s, %s, %s, now() + make_interval(secs => %s))"
                " on conflict (sheet_id) do update set run_id = excluded.run_id, holder = excluded.holder,"
                " acquired_at = now(), expires_at = excluded.expires_at"
                " where run_locks.expires_at < now()"
                " returning run_id::text as run_id, holder, expires_at",
                (sheet_id, run_id, holder, ttl_seconds)).fetchone()
            if row is not None:
                return LockState(True, row["run_id"], row["holder"], row["expires_at"])
            current = conn.execute("select run_id::text as run_id, holder, expires_at from run_locks"
                                   " where sheet_id = %s", (sheet_id,)).fetchone()
        return LockState(False, current["run_id"], current["holder"], current["expires_at"])

    def release_lock(self, sheet_id, run_id) -> bool:
        with self._connect() as conn:
            return conn.execute("delete from run_locks where sheet_id = %s and run_id = %s",
                                (sheet_id, run_id)).rowcount == 1

    def renew_lock(self, sheet_id, run_id, ttl_seconds) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "update run_locks set expires_at = now() + make_interval(secs => %s)"
                " where sheet_id = %s and run_id = %s and expires_at > now()",
                (ttl_seconds, sheet_id, run_id)).rowcount == 1

    def create_run(self, run: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into runs (run_id, sheet_id, close_period, started_by, role, auth_method,"
                " attribution_verified, status, settings, criteria_snapshot_id, question_builder_version,"
                " model_pin, jev_mode, lines_received) values"
                " (%s, %s, %s, %s, %s, %s, %s, 'running', %s, %s, %s, %s, %s, %s)",
                (run["run_id"], run["sheet_id"], run.get("close_period"), run.get("started_by"), run["role"],
                 run["auth_method"], run["attribution_verified"], Jsonb(run.get("settings", {})),
                 run.get("criteria_snapshot_id"), run["question_builder_version"], run["model_pin"],
                 run["jev_mode"], run.get("lines_received", 0)))

    def get_run(self, run_id):
        with self._connect() as conn:
            row = conn.execute(
                "select run_id::text as run_id, sheet_id, close_period, started_by, role, auth_method,"
                " attribution_verified, status, started_at, finished_at, stale_line_ids, written_count,"
                " lines_received, lines_evaluated, jev_requests, error from runs where run_id = %s",
                (run_id,)).fetchone()
        return dict(row) if row else None

    def finish_run(self, run_id, status, stale_line_ids=(), written_count=None, error=None) -> None:
        with self._connect() as conn:
            conn.execute(
                "update runs set status = %s, finished_at = now(), stale_line_ids = %s, written_count = %s,"
                " error = %s where run_id = %s",
                (status, list(stale_line_ids), written_count, error, run_id))

    def record_evaluation(self, sheet_id, run_id, lines: Sequence[LineIn], log_rows, counts) -> None:
        with self._connect() as conn:
            with conn.transaction():
                # The fence. Locking the lease row holds off a takeover (acquire_lock's
                # upsert waits on it) until this transaction commits or rolls back.
                lease = conn.execute(
                    "select run_id::text as run_id, expires_at > now() as live from run_locks"
                    " where sheet_id = %s for update", (sheet_id,)).fetchone()
                if lease is None or lease["run_id"] != str(run_id) or not lease["live"]:
                    raise LeaseLost("run {0} no longer holds the write lease for this sheet".format(run_id))
                for line in lines:
                    conn.execute(
                        "insert into lines (sheet_id, line_id, entity, close_period, comparison_basis, account,"
                        " slice, prior, current, variance_usd, variance_pct, first_seen_run)"
                        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " on conflict (sheet_id, line_id) do update set prior = excluded.prior,"
                        " current = excluded.current, variance_usd = excluded.variance_usd,"
                        " variance_pct = excluded.variance_pct, last_seen_at = now()",
                        (sheet_id, line.line_id, line.entity or "", line.line_id.split("|")[1],
                         line.basis or "", line.account or "",
                         line.line_id.split("|")[4] if line.line_id.count("|") == 4 else None,
                         line.prior, line.current, line.variance_usd, line.variance_pct, run_id))
                    text = line.commentary or ""
                    if text.strip():
                        conn.execute(
                            "insert into line_commentary (sheet_id, line_id, commentary_hash, commentary,"
                            " first_seen_run) values (%s, %s, %s, %s, %s) on conflict do nothing",
                            (sheet_id, line.line_id, commentary_hash(text), text, run_id))
                if log_rows:
                    with conn.cursor() as cur:
                        cur.executemany(
                            "insert into log (sheet_id, run_id, " + ", ".join(LOG_FIELDS) + ") values (%s, %s, "
                            + ", ".join(["%s"] * len(LOG_FIELDS)) + ")",
                            [[sheet_id, run_id] + [Jsonb(r.get(f)) if f == "detail" else r.get(f) for f in LOG_FIELDS]
                             for r in log_rows])
                conn.execute(
                    "update runs set lines_received = %s, lines_evaluated = %s, jev_requests = %s where run_id = %s",
                    (counts.get("lines_received", 0), counts.get("lines_evaluated", 0),
                     counts.get("jev_requests", 0), run_id))

    # reads -----------------------------------------------------------------
    def prior_attempts(self, sheet_id, line_ids: Iterable[str]) -> Dict[str, PriorAttempt]:
        ids = list(line_ids)
        if not ids:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                "with delivered as ("
                "  select l.id, l.line_id, l.run_id, l.attempt_number, l.commentary_hash, l.result"
                "  from log l join runs r on r.run_id = l.run_id"
                "  where l.sheet_id = %s and l.line_id = any(%s) and l.attempt_number >= 1"
                "    and not (l.line_id = any(r.stale_line_ids))"
                "), last_run as ("
                "  select distinct on (line_id) line_id, run_id from delivered order by line_id, id desc"
                ") select d.line_id, max(d.attempt_number) as attempt_number,"
                "  (array_agg(d.commentary_hash order by d.id desc))[1] as commentary_hash,"
                "  bool_or(d.result = 'fail') as failed"
                " from delivered d join last_run lr on lr.line_id = d.line_id and lr.run_id = d.run_id"
                " group by d.line_id", (sheet_id, ids)).fetchall()
        return {r["line_id"]: PriorAttempt(r["attempt_number"], r["commentary_hash"], r["failed"]) for r in rows}

    def log_rows(self, sheet_id, close_periods=None, limit=None):
        sql = ("select id, ts, sheet_id, run_id::text as run_id, " + ", ".join(LOG_FIELDS) +
               " from log where sheet_id = %s")
        params: List[Any] = [sheet_id]
        if close_periods:
            sql += " and close_period = any(%s)"
            params.append(list(close_periods))
        if limit:
            sql = "select * from (" + sql + " order by id desc limit %s) recent"
            params.append(limit)
        sql += " order by id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            row = dict(row)
            for key in ("jev_probability", "threshold"):
                if row.get(key) is not None:
                    row[key] = float(row[key])
            out.append(row)
        return out

    def close_periods(self, sheet_id):
        with self._connect() as conn:
            rows = conn.execute("select distinct close_period from log where sheet_id = %s order by 1",
                                (sheet_id,)).fetchall()
        return [r["close_period"] for r in rows]

    # overrides -------------------------------------------------------------
    def add_overrides(self, rows) -> int:
        inserted = 0
        with self._connect() as conn:
            with conn.transaction():
                for row in rows:
                    inserted += conn.execute(
                        "insert into overrides (sheet_id, close_period, line_id, kind, criterion_id, not_covered,"
                        " note, note_hash, submitted_by, role, attribution_verified, matched_flag)"
                        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
                        (row["sheet_id"], row["close_period"], row["line_id"], row["kind"], row.get("criterion_id"),
                         bool(row.get("not_covered")), row.get("note"), sha256_hex(row.get("note") or ""),
                         row.get("submitted_by"), row["role"], bool(row.get("attribution_verified")),
                         row.get("matched_flag"))).rowcount
        return inserted

    def overrides(self, sheet_id, close_periods=None):
        sql = ("select id, created_at, sheet_id, close_period, line_id, kind, criterion_id, not_covered, note,"
               " submitted_by, role, attribution_verified, matched_flag from overrides where sheet_id = %s")
        params: List[Any] = [sheet_id]
        if close_periods:
            sql += " and close_period = any(%s)"
            params.append(list(close_periods))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " order by id", params).fetchall()]

    # Jev cache -------------------------------------------------------------
    def jev_cache_get(self, fingerprint):
        with self._connect() as conn:
            row = conn.execute(
                "select probability, model_snapshot from jev_responses"
                " where fingerprint = %s and precision_generation = %s",
                (fingerprint, JEV_CACHE_GENERATION)).fetchone()
        return CachedAnswer(float(row["probability"]), row["model_snapshot"]) if row else None

    def jev_cache_put(self, fingerprint, question_id, answer: JevAnswer, usage, source="live") -> None:
        tokens = usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else None
        cost = usage.get("cost")
        with self._connect() as conn:
            # The probability goes in as received: a cached rerun must grade exactly as the live run did.
            conn.execute(
                "insert into jev_responses (fingerprint, precision_generation, model_snapshot, question_id,"
                " probability, source, input_tokens, cost_usd) values (%s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict do nothing",
                (fingerprint, JEV_CACHE_GENERATION, answer.model, question_id, float(answer.probability), source,
                 tokens, cost if isinstance(cost, (int, float)) else None))

    # health ----------------------------------------------------------------
    def save_health(self, sheet_id, health_run_id, rows, generated_by) -> None:
        with self._connect() as conn:
            with conn.transaction():
                for row in rows:
                    conn.execute(
                        "insert into criteria_health (health_run_id, sheet_id, close_window, criterion_id, signal,"
                        " evidence, suggestion, generated_by) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (health_run_id, sheet_id, row["close_window"], row["criterion_id"], row["signal"],
                         Jsonb(row.get("evidence", {})), row["suggestion"], generated_by))

    def resolve_health(self, sheet_id, resolutions, resolved_by) -> int:
        updated = 0
        with self._connect() as conn:
            for item in resolutions:
                updated += conn.execute(
                    "update criteria_health set resolution = %s, resolved_by = %s, resolved_at = now()"
                    " where sheet_id = %s and id = %s", (item["resolution"], resolved_by, sheet_id, item["id"])
                ).rowcount
        return updated

    def health_rows(self, sheet_id, health_run_id=None):
        with self._connect() as conn:
            if health_run_id is None:
                latest = conn.execute("select health_run_id::text as hid from criteria_health where sheet_id = %s"
                                      " order by id desc limit 1", (sheet_id,)).fetchone()
                if latest is None:
                    return []
                health_run_id = latest["hid"]
            rows = conn.execute(
                "select id, health_run_id::text as health_run_id, close_window, criterion_id, signal, evidence,"
                " suggestion, generated_at, generated_by, resolution, resolved_by, resolved_at"
                " from criteria_health where sheet_id = %s and health_run_id = %s order by id",
                (sheet_id, health_run_id)).fetchall()
        return [dict(r) for r in rows]
