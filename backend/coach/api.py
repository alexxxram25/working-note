"""The Coach service: what the sheet's script calls.

    GET  /healthz                     liveness plus the posture that matters (store, auth, Jev pin)
    GET  /v1/me                       who the service thinks you are, and your role
    GET  /v1/team                     active reviewers and preparers (reviewer), for sheet protections
    GET  /v1/defaults                 the shipped Criteria and Settings, for an empty sheet
    POST /v1/runs                     take the write lease, check every line, log, return results
    POST /v1/runs/{run_id}/complete   report which lines were written and which changed mid-run; free the lease
    POST /v1/overrides                record "flag was wrong" and "missed an issue", stamped with the caller
    POST /v1/criteria/status          the service's own calibration status per criterion (reviewer)
    POST /v1/health                   Criteria Health and the second-attempts view (reviewer)

Server to server only: the sheet's script calls this with UrlFetchApp, never a
browser, so there is no CORS middleware at all. Errors are JSON with a request
ID; nothing in a response or a log line contains commentary text or a secret.
"""

from __future__ import annotations

import collections
import json
import logging
import sys
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from . import __version__, criteria_versions, pipeline
from .auth import Authenticator, AuthError, GoogleVerifier, Identity, require_reviewer, require_sheet
from .config import Config
from .health import health_report
from .jev.client import LiveJevClient
from .jev.questions import QUESTION_BUILDER_VERSION
from .jev.replay import CachingJevClient, ReplayJevClient, load_recordings
from .jev.thresholds import CALIBRATED, UNCALIBRATED, CalibrationIndex
from .line_ids import LineIdError, parse_line_id
from .models import Criterion, LineIn, Settings
from .seed import seed_defaults, settings_rows
from .store.base import LeaseLost, Store, StoreJevCache
from .store.memory import MemoryStore

log = logging.getLogger("coach")

OVERRIDE = re.compile(r"^\s*(Flag was wrong|Missed an issue)\s*:\s*(.+?)\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


@dataclass
class Services:
    config: Config
    store: Store
    jev: Any
    auth: Authenticator
    recordings: int
    known_snapshots: Tuple[str, ...]


class ConfigError(RuntimeError):
    pass


def build_services(config: Config) -> Services:
    problems = config.problems()
    if problems:
        raise ConfigError("refusing to start:\n  - " + "\n  - ".join(problems))

    if config.store == "postgres":
        from .store.postgres import PostgresStore

        store: Store = PostgresStore(config.database_url)
    else:
        store = MemoryStore()
        seed_defaults(store)

    # Replay mode answers from recorded Jev answers (tests and local runs) and
    # fails loudly on a question nobody recorded. Live mode asks Jev itself.
    recordings = load_recordings(config.jev_recordings) if config.jev_mode == "replay" and config.jev_recordings else {}
    recordings = {fp: r for fp, r in recordings.items() if r.request_model == config.jev_model}
    known = tuple(config.jev_allowed_snapshots) or tuple(sorted({r.model for r in recordings.values()}))

    if config.jev_mode == "live":
        live = LiveJevClient(config.jev_provider, config.api_key(), config.jev_model,
                             config.jev_allowed_snapshots, data_collection=config.jev_data_collection)
        jev: Any = CachingJevClient(live, StoreJevCache(store), config.jev_model, config.jev_allowed_snapshots)
    else:
        jev = ReplayJevClient(recordings, config.jev_model, config.jev_allowed_snapshots)

    google = None
    if "google" in config.auth_modes:
        google = GoogleVerifier(config.google_audiences, config.google_jwks_url, config.google_hosted_domains)
    auth = Authenticator(store, google, sheet_keys="sheet_key" in config.auth_modes,
                         allow_unlisted=config.allow_unlisted, local="local" in config.auth_modes)
    return Services(config, store, jev, auth, len(recordings), known)


class RateLimiter:
    """Sliding one-minute window per caller. In memory: Railway runs one instance."""

    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 60.0:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    sheet_id: str = Field(min_length=1, max_length=200)
    client_version: Optional[str] = Field(default=None, max_length=64)
    settings: List[Dict[str, Any]] = Field(default_factory=list, max_length=200)
    criteria: List[Dict[str, Any]] = Field(min_length=1, max_length=100)
    criteria_edit_hints: Dict[str, Dict[str, Optional[str]]] = Field(default_factory=dict)
    lines: List[LineIn] = Field(min_length=1, max_length=2000)


class CompleteRequest(BaseModel):
    sheet_id: str = Field(min_length=1, max_length=200)
    written: List[str] = Field(default_factory=list, max_length=5000)
    stale: List[str] = Field(default_factory=list, max_length=5000)


class OverrideIn(BaseModel):
    line_id: str = Field(min_length=1, max_length=300)
    value: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=1000)


class OverridesRequest(BaseModel):
    sheet_id: str = Field(min_length=1, max_length=200)
    overrides: List[OverrideIn] = Field(default_factory=list, max_length=5000)


class CriteriaStatusRequest(BaseModel):
    sheet_id: str = Field(min_length=1, max_length=200)
    criteria: List[Dict[str, Any]] = Field(min_length=1, max_length=100)


class Resolution(BaseModel):
    id: int
    resolution: str = Field(pattern="^(edit|retire|keep)$")


class HealthRequest(BaseModel):
    sheet_id: str = Field(min_length=1, max_length=200)
    criteria: List[Dict[str, Any]] = Field(min_length=1, max_length=100)
    settings: List[Dict[str, Any]] = Field(default_factory=list, max_length=200)
    resolutions: List[Resolution] = Field(default_factory=list, max_length=500)
    include_log: bool = False
    log_limit: int = Field(default=2000, ge=1, le=20000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _criteria(rows: List[Dict[str, Any]], require_active: bool = False) -> List[Criterion]:
    out: List[Criterion] = []
    problems = []
    for index, row in enumerate(rows, start=2):
        if not str(row.get("ID", "")).strip():
            continue  # a blank row in the tab
        try:
            out.append(Criterion.from_sheet_row(row))
        except (ValidationError, ValueError) as exc:
            problems.append("Criteria row {0}: {1}".format(index, _first_error(exc)))
    ids = [c.id for c in out]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        problems.append("Criteria IDs appear more than once: {0}".format(", ".join(duplicates)))
    if problems:
        raise HTTPException(422, detail={"message": "Fix the Criteria tab", "problems": problems})
    if not out:
        raise HTTPException(422, detail={"message": "The Criteria tab has no criteria", "problems": []})
    if require_active and not any(c.active for c in out):
        # Every check switched off would grade every line "Passed" having checked nothing.
        raise HTTPException(422, detail={
            "message": "The Criteria tab has no active criteria",
            "problems": ["Every criterion is marked N in Active (Y/N), so nothing would be checked. "
                         "Set at least one criterion to Y."]})
    return out


def _settings(rows: List[Dict[str, Any]], store: Store) -> Settings:
    try:
        if rows:
            return Settings.from_sheet_rows(rows)
        return Settings(**store.default_settings())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, detail={"message": "Fix the Settings tab", "problems": [_first_error(exc)]})


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        err = exc.errors()[0]
        return "{0}: {1}".format(".".join(str(p) for p in err.get("loc", ())), err.get("msg"))
    return str(exc)


def _status_for(services: Services, criterion: Criterion, calibration: CalibrationIndex) -> str:
    if criterion.kind == "deterministic":
        return "Deterministic"
    if criterion.kind == "heuristic":
        return "Heuristic"
    snapshots = services.known_snapshots
    if any(calibration.is_calibrated(criterion, s)[0] for s in snapshots):
        return CALIBRATED
    return UNCALIBRATED


def _holder_label(holder: Optional[str]) -> str:
    if not holder:
        return "someone"
    return holder.split(":", 1)[1] if ":" in holder else holder


def _parse_override(value: str) -> Tuple[str, Optional[str], bool]:
    match = OVERRIDE.match(value or "")
    if not match:
        raise ValueError("not an override value: expected 'Flag was wrong: <ID>' or 'Missed an issue: <ID>'")
    kind = "flag_wrong" if match.group(1).lower().startswith("flag") else "missed_issue"
    target = match.group(2).strip()
    if target.lower() == "not covered":
        if kind == "flag_wrong":
            raise ValueError("'not covered' only applies to 'Missed an issue'")
        return kind, None, True
    return kind, target, False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(config: Optional[Config] = None, services: Optional[Services] = None) -> FastAPI:
    if services is None:
        services = build_services(config or Config.from_env())
    config = services.config
    limiter = RateLimiter()
    app = FastAPI(title="Working Note", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None if config.production else "/openapi.json")
    app.state.services = services

    @app.middleware("http")
    async def guard(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.monotonic()
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > config.max_body_bytes:
            response = JSONResponse(status_code=413, content={
                "detail": "Request body over {0} bytes".format(config.max_body_bytes), "request_id": request_id})
        else:
            try:
                response = await call_next(request)
            except Exception:  # noqa: BLE001 - convert, never leak a stack trace
                log.exception("unhandled error request_id=%s path=%s", request_id, request.url.path)
                response = JSONResponse(status_code=500, content={
                    "detail": "The Coach service hit an internal error.", "request_id": request_id})
        response.headers["X-Request-ID"] = request_id
        log.info(json.dumps({
            "level": "info", "request_id": request_id, "method": request.method, "path": request.url.path,
            "status": response.status_code, "ms": int((time.monotonic() - started) * 1000),
        }))
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # FastAPI's default handler echoes the offending input, which here may be
        # commentary text, and fails outright on NaN (not JSON). Report where and
        # why, never what: the validators put the value after the first ': '
        # (models.parse_number), and everything from there on is dropped.
        problems = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", ()) if p != "body"]
            reason = str(err.get("msg", "invalid")).split(": ", 1)[0]
            problems.append("{0}: {1}".format(".".join(loc) or "body", reason))
        return JSONResponse(status_code=422, content={
            "detail": {"message": "The request could not be read", "problems": problems},
            "request_id": getattr(request.state, "request_id", None)})

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        return JSONResponse(status_code=exc.status, content={
            "detail": str(exc), "request_id": getattr(request.state, "request_id", None)})

    def identity(
        authorization: Optional[str] = Header(default=None),
        x_coach_sheet_key: Optional[str] = Header(default=None),
        x_coach_user: Optional[str] = Header(default=None),
    ) -> Identity:
        who = services.auth.authenticate(authorization, x_coach_sheet_key, x_coach_user)
        limit = who.rate_limit_per_minute or config.rate_limit_per_minute
        if not limiter.allow(who.principal, limit):
            raise HTTPException(429, detail="Too many requests from this caller; wait a minute and retry.")
        return who

    @app.get("/healthz")
    def healthz():
        try:
            store_ok = services.store.ping()
        except Exception:  # noqa: BLE001
            store_ok = False
        body = {
            "ok": store_ok,
            "service": "working-note",
            "version": __version__,
            "store_ok": store_ok,
            "question_builder_version": QUESTION_BUILDER_VERSION,
            "recordings": services.recordings,
            "known_snapshots": list(services.known_snapshots),
        }
        body.update(config.summary())
        return JSONResponse(status_code=200 if store_ok else 503, content=body)

    @app.get("/v1/me")
    def me(who: Identity = Depends(identity)):
        return {"email": who.email, "role": who.role, "method": who.method, "verified": who.verified,
                "display_name": who.display_name, "sheet_scope": who.sheet_scope, "service": config.summary()}

    @app.get("/v1/team")
    def team(who: Identity = Depends(identity)):
        require_reviewer(who)
        members = [m for m in services.store.team_members() if m.active]
        return {"reviewers": [m.email for m in members if m.role == "reviewer"],
                "preparers": [m.email for m in members if m.role == "preparer"]}

    @app.get("/v1/defaults")
    def defaults(who: Identity = Depends(identity)):
        return {"criteria": services.store.default_criteria(),
                "settings": settings_rows(services.store.default_settings())}

    @app.post("/v1/runs")
    def start_run(body: RunRequest, who: Identity = Depends(identity)):
        return do_run(body, who)

    def do_run(body: RunRequest, who: Identity):
        require_sheet(who, body.sheet_id)
        criteria = _criteria(body.criteria, require_active=True)
        settings = _settings(body.settings, services.store)
        if settings.pinned_model_version != config.jev_pinned_version:
            raise HTTPException(409, detail=(
                "Settings pins {0} but this service is pinned to {1}. The pin is changed by redeploying "
                "the service, not from the sheet; fix the Settings value to match.".format(
                    settings.pinned_model_version, config.jev_pinned_version)))

        run_id = str(uuid.uuid4())
        lease = services.store.acquire_lock(body.sheet_id, run_id, who.principal, config.lock_ttl_seconds)
        if not lease.acquired:
            return JSONResponse(status_code=409, content={
                "detail": "Another Coach run is in progress on this sheet (started by {0}). It frees up by {1} "
                          "at the latest; try again shortly.".format(_holder_label(lease.holder),
                                                                     lease.expires_at.strftime("%H:%M:%S UTC")),
                "holder": _holder_label(lease.holder), "expires_at": lease.expires_at.isoformat()})
        created = False
        try:
            store = services.store
            latest, state = store.criteria_state(body.sheet_id)
            digest, content = criteria_versions.snapshot(criteria)
            # Compare the criteria themselves, not only the snapshot pointer: a
            # service from before criteria_current existed advanced the history
            # without moving the pointer, so the pointer alone can say "unchanged"
            # while the latest recorded version describes a different tab.
            changes = criteria_versions.changes(state, criteria)
            changed_by = {cid: (hint or {}).get("by") for cid, hint in body.criteria_edit_hints.items()
                          if (hint or {}).get("by")}
            snapshot_id = None
            if digest != latest or changes:
                snapshot_id = store.record_criteria(body.sheet_id, digest, content, changes, changed_by,
                                                    who.attribution, run_id)
            versions = {cid: v for cid, (v, _) in store.criteria_state(body.sheet_id)[1].items()}

            periods = collections.Counter()
            for line in body.lines:
                try:
                    periods[parse_line_id(line.line_id).period] += 1
                except LineIdError:
                    pass
            store.create_run({
                "run_id": run_id, "sheet_id": body.sheet_id,
                "close_period": periods.most_common(1)[0][0] if periods else None,
                "started_by": who.attribution, "role": who.role, "auth_method": who.method,
                "attribution_verified": who.verified, "settings": settings.model_dump(),
                "criteria_snapshot_id": snapshot_id, "question_builder_version": QUESTION_BUILDER_VERSION,
                "model_pin": config.jev_pinned_version, "jev_mode": config.jev_mode,
                "lines_received": len(body.lines),
            })
            created = True
            calibration = CalibrationIndex(store.calibration_records())
            prior = store.prior_attempts(body.sheet_id, [l.line_id for l in body.lines])
            output = pipeline.run(pipeline.PipelineConfig(
                criteria=criteria, settings=settings, calibration=calibration, jev=services.jev,
                criteria_versions=versions, jev_concurrency=config.jev_concurrency, session_id=run_id,
                heartbeat=_lease_heartbeat(store, body.sheet_id, run_id, config.lock_ttl_seconds),
            ), body.lines, prior)
            rejected_ids = {r["line_id"] for r in output.rejected}
            evaluated = [l for l in body.lines if l.line_id not in rejected_ids]
            store.record_evaluation(body.sheet_id, run_id, evaluated, output.log_rows, {
                "lines_received": len(body.lines),
                "lines_evaluated": sum(1 for r in output.results if r.material),
                "jev_requests": output.jev_requests,
            })
        except LeaseLost:
            # The lease lapsed or was taken over while this run was evaluating. Its
            # results describe text another run may since have graded: discard them.
            if created:
                try:
                    services.store.finish_run(run_id, "failed", error="LeaseLost")
                except Exception:  # noqa: BLE001
                    log.exception("could not mark run %s failed", run_id)
            services.store.release_lock(body.sheet_id, run_id)
            raise HTTPException(409, detail=(
                "This run took longer than its write lease and another run may have started since; its "
                "results were discarded and nothing was written. Run Coach again."))
        except Exception as exc:
            if created:
                try:
                    services.store.finish_run(run_id, "failed", error=type(exc).__name__)
                except Exception:  # noqa: BLE001
                    log.exception("could not mark run %s failed", run_id)
            services.store.release_lock(body.sheet_id, run_id)
            raise

        statuses = collections.Counter(r.status for r in output.results)
        return {
            "run_id": run_id,
            "lock_expires_at": lease.expires_at.isoformat(),
            "results": [r.model_dump() for r in output.results],
            "rejected": output.rejected,
            "criteria_status": [
                {"id": c.id, "version": versions.get(c.id), "calibration_status": _status_for(services, c, calibration)}
                for c in criteria
            ],
            "criteria_changes": [dict(criteria_versions.describe(c), changed_by=changed_by.get(c.criterion_id),
                                      detected_by=who.attribution) for c in changes],
            "summary": {
                "lines": len(output.results), "rejected": len(output.rejected),
                "passed": statuses.get("passed", 0), "needs_revision": statuses.get("needs_revision", 0),
                "needs_commentary": statuses.get("needs_commentary", 0),
                "skipped_immaterial": statuses.get("skipped_immaterial", 0),
                "reviewer_prompt": statuses.get("reviewer_prompt", 0), "errors": statuses.get("error", 0),
                "jev_requests": output.jev_requests, "lines_sent_to_jev": len(output.lines_sent_to_jev),
            },
            "model": {"pinned": config.jev_pinned_version, "request": config.jev_model, "mode": config.jev_mode},
        }

    @app.post("/v1/runs/{run_id}/complete")
    def complete_run(run_id: str, body: CompleteRequest, who: Identity = Depends(identity)):
        return do_complete(run_id, body, who)

    def do_complete(run_id: str, body: CompleteRequest, who: Identity):
        require_sheet(who, body.sheet_id)
        try:
            uuid.UUID(run_id)
        except ValueError:
            raise HTTPException(404, detail="no such run")
        run = services.store.get_run(run_id)
        if run is None or run["sheet_id"] != body.sheet_id:
            raise HTTPException(404, detail="no such run")
        if run.get("started_by") != who.attribution or run.get("auth_method") != who.method:
            raise HTTPException(403, detail="only the caller who started a run can complete it")
        if run["status"] == "running":
            services.store.finish_run(run_id, "completed", stale_line_ids=sorted(set(body.stale)),
                                      written_count=len(body.written))
        released = services.store.release_lock(body.sheet_id, run_id)
        return {"run_id": run_id, "released": released, "stale": sorted(set(body.stale)),
                "written": len(body.written)}

    @app.post("/v1/overrides")
    def overrides(body: OverridesRequest, who: Identity = Depends(identity)):
        return do_overrides(body, who)

    def do_overrides(body: OverridesRequest, who: Identity):
        require_sheet(who, body.sheet_id)
        rows, invalid = [], []
        failed_by_line: Dict[str, set] = {}
        periods = set()
        for item in body.overrides:
            try:
                periods.add(parse_line_id(item.line_id).period)
            except LineIdError:
                pass
        if periods:
            # Which criteria failed on each line's most recent checked run: a
            # "flag was wrong" on a criterion that never flagged the line is noise.
            last_run: Dict[str, str] = {}
            fails: Dict[Tuple[str, str], set] = collections.defaultdict(set)
            for row in services.store.log_rows(body.sheet_id, sorted(periods)):
                if row["attempt_number"] < 1:
                    continue
                last_run[row["line_id"]] = row["run_id"]
                if row["result"] == "fail":
                    fails[(row["line_id"], row["run_id"])].add(row["criterion_id"])
            failed_by_line = {lid: fails.get((lid, rid), set()) for lid, rid in last_run.items()}
        for item in body.overrides:
            try:
                period = parse_line_id(item.line_id).period
                kind, criterion_id, not_covered = _parse_override(item.value)
            except (LineIdError, ValueError) as exc:
                invalid.append({"line_id": item.line_id, "value": item.value, "error": str(exc)})
                continue
            rows.append({
                "sheet_id": body.sheet_id, "close_period": period, "line_id": item.line_id, "kind": kind,
                "criterion_id": criterion_id, "not_covered": not_covered, "note": (item.note or "").strip() or None,
                "submitted_by": who.attribution, "role": who.role, "attribution_verified": who.verified,
                "matched_flag": (criterion_id in failed_by_line.get(item.line_id, set()))
                if kind == "flag_wrong" else None,
            })
        recorded = services.store.add_overrides(rows)
        return {"received": len(body.overrides), "recorded": recorded, "already_recorded": len(rows) - recorded,
                "invalid": invalid, "attributed_to": who.attribution, "attribution_verified": who.verified}

    @app.post("/v1/criteria/status")
    def criteria_status(body: CriteriaStatusRequest, who: Identity = Depends(identity)):
        require_sheet(who, body.sheet_id)
        require_reviewer(who)
        criteria = _criteria(body.criteria)
        calibration = CalibrationIndex(services.store.calibration_records())
        _, state = services.store.criteria_state(body.sheet_id)
        return {"criteria": [
            {"id": c.id, "calibration_status": _status_for(services, c, calibration),
             "version": state.get(c.id, (None,))[0]} for c in criteria]}

    @app.post("/v1/health")
    def health(body: HealthRequest, who: Identity = Depends(identity)):
        require_sheet(who, body.sheet_id)
        require_reviewer(who)
        criteria = _criteria(body.criteria)
        settings = _settings(body.settings, services.store)
        resolved = 0
        if body.resolutions:
            resolved = services.store.resolve_health(body.sheet_id, [r.model_dump() for r in body.resolutions],
                                                     who.attribution)
        report = health_report(services.store, body.sheet_id, criteria, settings)
        health_run_id = str(uuid.uuid4())
        services.store.save_health(body.sheet_id, health_run_id, report["prompts"], who.attribution)
        stored = services.store.health_rows(body.sheet_id, health_run_id)
        for prompt, row in zip(report["prompts"], stored):
            prompt["id"] = row["id"]
        response = dict(report, health_run_id=health_run_id, resolutions_recorded=resolved,
                        history=[_history_row(h) for h in services.store.criteria_history(body.sheet_id)])
        if body.include_log:
            response["log"] = [_log_row(r) for r in services.store.log_rows(body.sheet_id, limit=body.log_limit)]
        return response

    return app


def _lease_heartbeat(store: Store, sheet_id: str, run_id: str, ttl_seconds: int):
    """Renew the write lease as Jev answers arrive, at most every third of its TTL.

    Renewal is one UPDATE, so it is cheap; and a lease that can no longer be
    renewed is a lease lost, reported now rather than after every line was graded.
    """
    interval = max(1.0, ttl_seconds / 3.0)
    last = [time.monotonic()]

    def beat() -> None:
        now = time.monotonic()
        if now - last[0] < interval:
            return
        last[0] = now
        if not store.renew_lock(sheet_id, run_id, ttl_seconds):
            raise LeaseLost("run {0} could not renew the write lease for this sheet".format(run_id))

    return beat


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _history_row(row: Dict[str, Any]) -> Dict[str, Any]:
    described = criteria_versions.describe(criteria_versions.Change(
        row["criterion_id"], row["version"], row.get("content") or {}, row.get("content_hash", ""),
        list(row.get("fields_changed") or []), dict(row.get("diff") or {}), row.get("calibration_impact", "")))
    described.update(changed_at=_iso(row.get("created_at")), changed_by=row.get("changed_by"),
                     detected_by=row.get("detected_by"))
    return described


def _log_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _iso(row.get(k)) for k in (
        "ts", "close_period", "line_id", "run_id", "attempt_number", "commentary_hash", "criterion_id", "result",
        "jev_probability", "criterion_version", "question_builder_version", "model_version", "calibration_status")}


def configure_logging() -> List[logging.Handler]:
    """Info to stdout, warnings and errors to stderr.

    Railway (like most hosts) files everything on stderr as an error, so with
    Python's default handler every request line showed up red.
    """
    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.INFO)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    for handler in (out, err):
        handler.setFormatter(logging.Formatter("%(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[out, err], force=True)
    return [out, err]


def _app_from_env() -> FastAPI:
    from . import envfile

    envfile.load()
    configure_logging()
    return create_app()


class _LazyApp:
    """`uvicorn coach.api:app` builds the app on first use, so importing this
    module (tests, scripts) never reads the environment or opens a database."""

    _app: Optional[FastAPI] = None

    async def __call__(self, scope, receive, send):
        if _LazyApp._app is None:
            _LazyApp._app = _app_from_env()
        await _LazyApp._app(scope, receive, send)


app = _LazyApp()
