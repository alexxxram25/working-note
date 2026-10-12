"""The storage contract the service is written against."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from ..attempts import PriorAttempt
from ..criteria_versions import Change
from ..jev.client import JevAnswer
from ..jev.replay import CachedAnswer
from ..models import LineIn

LOG_FIELDS = (
    "close_period", "line_id", "attempt_number", "commentary_hash", "criterion_id", "result",
    "jev_probability", "criterion_version", "question_builder_version", "model_version",
    "calibration_status", "jev_source", "threshold", "detail",
)


class TeamMember(NamedTuple):
    email: str
    role: str
    display_name: Optional[str] = None
    active: bool = True


class SheetKey(NamedTuple):
    key_id: str
    key_hash: str
    sheet_id: str
    role_ceiling: str
    label: Optional[str] = None
    rate_limit_per_minute: int = 30
    revoked_at: Optional[dt.datetime] = None


class LockState(NamedTuple):
    acquired: bool
    run_id: str
    holder: Optional[str]
    expires_at: dt.datetime


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class LeaseLost(RuntimeError):
    """The run's write lease is not the live one for its sheet: it expired, was
    released, or another run took it over. Nothing was written."""


class Store(ABC):
    kind = "abstract"

    # team and keys -------------------------------------------------------
    @abstractmethod
    def team_member(self, email: str) -> Optional[TeamMember]: ...

    @abstractmethod
    def upsert_team_member(self, email: str, role: str, display_name: Optional[str] = None,
                           active: bool = True) -> None: ...

    @abstractmethod
    def team_members(self) -> List[TeamMember]: ...

    @abstractmethod
    def sheet_key(self, key_hash: str) -> Optional[SheetKey]: ...

    @abstractmethod
    def add_sheet_key(self, key: SheetKey, created_by: Optional[str] = None) -> None: ...

    @abstractmethod
    def revoke_sheet_key(self, key_id: str) -> bool: ...

    @abstractmethod
    def touch_sheet_key(self, key_id: str) -> None: ...

    # defaults --------------------------------------------------------------
    @abstractmethod
    def default_settings(self) -> Dict[str, Any]: ...

    @abstractmethod
    def default_criteria(self) -> List[Dict[str, Any]]: ...

    # calibration -----------------------------------------------------------
    @abstractmethod
    def calibration_records(self) -> List[Tuple[str, str, str]]: ...

    @abstractmethod
    def add_calibration(self, record: Dict[str, Any]) -> None: ...

    # criteria versions -----------------------------------------------------
    @abstractmethod
    def criteria_state(self, sheet_id: str) -> Tuple[Optional[str], Dict[str, Tuple[int, Dict[str, str]]]]:
        """(latest snapshot hash, {criterion: (version, content)})."""

    @abstractmethod
    def record_criteria(self, sheet_id: str, snapshot_hash: str, content: List[Dict[str, str]],
                        changes: Sequence[Change], changed_by: Dict[str, str], detected_by: Optional[str],
                        run_id: Optional[str]) -> Optional[int]: ...

    @abstractmethod
    def criteria_history(self, sheet_id: str, limit: int = 500) -> List[Dict[str, Any]]: ...

    # runs and the write lease ---------------------------------------------
    @abstractmethod
    def acquire_lock(self, sheet_id: str, run_id: str, holder: Optional[str], ttl_seconds: int) -> LockState: ...

    @abstractmethod
    def release_lock(self, sheet_id: str, run_id: str) -> bool: ...

    @abstractmethod
    def renew_lock(self, sheet_id: str, run_id: str, ttl_seconds: int) -> bool:
        """Push the expiry out, only if this run still holds a live lease. False if it does not."""

    @abstractmethod
    def create_run(self, run: Dict[str, Any]) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def finish_run(self, run_id: str, status: str, stale_line_ids: Sequence[str] = (),
                   written_count: Optional[int] = None, error: Optional[str] = None) -> None: ...

    @abstractmethod
    def record_evaluation(self, sheet_id: str, run_id: str, lines: Sequence[LineIn],
                          log_rows: Sequence[Dict[str, Any]], counts: Dict[str, int]) -> None:
        """Lines, commentary versions, Log rows and run counts, all or nothing.

        Writes only while `run_id` holds the sheet's live lease, checked inside the
        same transaction; otherwise raises LeaseLost and writes nothing. A run whose
        lease lapsed may have been superseded by one that already graded newer text.
        """

    # reads -----------------------------------------------------------------
    @abstractmethod
    def prior_attempts(self, sheet_id: str, line_ids: Iterable[str]) -> Dict[str, PriorAttempt]: ...

    @abstractmethod
    def log_rows(self, sheet_id: str, close_periods: Optional[Sequence[str]] = None,
                 limit: Optional[int] = None) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def close_periods(self, sheet_id: str) -> List[str]: ...

    # overrides -------------------------------------------------------------
    @abstractmethod
    def add_overrides(self, rows: Sequence[Dict[str, Any]]) -> int: ...

    @abstractmethod
    def overrides(self, sheet_id: str, close_periods: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]: ...

    # Jev cache -------------------------------------------------------------
    @abstractmethod
    def jev_cache_get(self, fingerprint: str) -> Optional[CachedAnswer]: ...

    @abstractmethod
    def jev_cache_put(self, fingerprint: str, question_id: str, answer: JevAnswer,
                      usage: Dict[str, Any], source: str = "live") -> None: ...

    # Criteria Health -------------------------------------------------------
    @abstractmethod
    def save_health(self, sheet_id: str, health_run_id: str, rows: Sequence[Dict[str, Any]],
                    generated_by: Optional[str]) -> None: ...

    @abstractmethod
    def resolve_health(self, sheet_id: str, resolutions: Sequence[Dict[str, Any]],
                       resolved_by: Optional[str]) -> int: ...

    @abstractmethod
    def health_rows(self, sheet_id: str, health_run_id: Optional[str] = None) -> List[Dict[str, Any]]: ...

    def ping(self) -> bool:
        return True


class StoreJevCache:
    """Adapts a Store to the JevCache protocol used by CachingJevClient."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, fingerprint: str) -> Optional[CachedAnswer]:
        return self.store.jev_cache_get(fingerprint)

    def put(self, fingerprint: str, question_id: str, answer: JevAnswer, usage: Dict[str, Any]) -> None:
        self.store.jev_cache_put(fingerprint, question_id, answer, usage)
