"""Shared types: criteria, settings, lines in, results out.

Values arrive from a spreadsheet, so the validators here are forgiving about
form ("Y", "yes", True; "10%", 0.1, 10; "$25,000") and strict about meaning (a
threshold must be a probability; a materiality % must be a fraction once read).
"""

from __future__ import annotations

import math
import re
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import sheet_schema as schema

MATERIALITY_ID = "MATERIALITY"
COMMENTARY_ID = "COMMENTARY"
SYSTEM_KINDS = {"ARITH": "deterministic", "DUP": "deterministic", "NEAR": "heuristic"}

_NUMBER_JUNK = re.compile(r"[,$\s]")


def parse_number(value: Any) -> Optional[float]:
    """Spreadsheet numbers: '', None and 'n/a' are missing; '(1,200)' is negative.

    NaN and the infinities are refused, not passed through: JSON parsers accept
    them, a comparison against one is quietly false, and int() of one is a crash.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:  # an int too large for a float; pydantic only converts ValueError
            raise ValueError("not a finite number: {0!r}".format(value))
    else:
        text = str(value).strip()
        if not text or text.lower() in ("n/a", "na", "-", "none"):
            return None
        negative = text.startswith("(") and text.endswith(")")
        text = _NUMBER_JUNK.sub("", text.strip("()"))
        percent = text.endswith("%")
        text = text.rstrip("%")
        try:
            number = float(text)
        except ValueError:
            raise ValueError("not a number: {0!r}".format(value))
        if percent:
            number /= 100.0
        if negative:
            number = -number
    if not math.isfinite(number):
        raise ValueError("not a finite number: {0!r}".format(value))
    return number


def parse_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("y", "yes", "true", "1", "active", "on"):
        return True
    if text in ("n", "no", "false", "0", "inactive", "off", "retired"):
        return False
    raise ValueError("expected Y or N, got {0!r}".format(value))


class Criterion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    passes_when: str = ""
    feedback: str = ""
    why: str = ""
    example: str = ""
    owner: str = ""
    last_reviewed: Optional[str] = None
    threshold: Optional[float] = None
    calibration_status: str = ""
    active: bool = True

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        value = (value or "").strip()
        if not value or "|" in value or ":" in value:
            raise ValueError("criterion ID must be non-empty and contain no '|' or ':'")
        return value

    @field_validator("threshold", mode="before")
    @classmethod
    def _threshold(cls, value: Any) -> Optional[float]:
        if isinstance(value, str) and value.strip().lower() in ("", "n/a", "settings", "na"):
            return None
        number = parse_number(value)
        if number is None:
            return None
        if not 0.0 < number < 1.0:
            raise ValueError("threshold must be between 0 and 1, got {0}".format(number))
        # A calibration is identified by the threshold to three decimals (jev/questions.py);
        # a fourth decimal would grade one way and be looked up as another.
        if abs(number - round(number, 3)) > 1e-9:
            raise ValueError("threshold may have at most three decimals, got {0!r}".format(number))
        return number

    @field_validator("active", mode="before")
    @classmethod
    def _active(cls, value: Any) -> bool:
        return parse_flag(value)

    @field_validator("last_reviewed", mode="before")
    @classmethod
    def _last_reviewed(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text[:10] if text else None

    @property
    def kind(self) -> str:
        return SYSTEM_KINDS.get(self.id, "jev")

    SHEET_FIELDS: ClassVar[Dict[str, str]] = {
        "ID": "id",
        "Criterion": "name",
        "Passes when": "passes_when",
        "Feedback when failed": "feedback",
        "Why it matters": "why",
        "Example of good": "example",
        "Owner": "owner",
        "Last reviewed": "last_reviewed",
        "Threshold": "threshold",
        "Calibration status": "calibration_status",
        "Active (Y/N)": "active",
    }

    @classmethod
    def from_sheet_row(cls, row: Dict[str, Any]) -> "Criterion":
        data = {field: row.get(column) for column, field in cls.SHEET_FIELDS.items() if column in row}
        for key in ("name", "passes_when", "feedback", "why", "example", "owner", "calibration_status"):
            if data.get(key) is None:
                data.pop(key, None)
            else:
                data[key] = str(data[key])
        if data.get("active") in (None, ""):
            data["active"] = True
        return cls(**data)

    def sheet_content(self) -> Dict[str, str]:
        """The fields whose change makes a new criterion version.

        Last reviewed and Calibration status are status, not content: re-confirming
        a criterion or recalibrating it does not make it a different criterion.
        """
        return {
            "ID": self.id,
            "Criterion": self.name,
            "Passes when": self.passes_when,
            "Feedback when failed": self.feedback,
            "Why it matters": self.why,
            "Example of good": self.example,
            "Owner": self.owner,
            "Threshold": "" if self.threshold is None else "{0:.3f}".format(self.threshold),
            "Active (Y/N)": "Y" if self.active else "N",
        }


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    materiality_usd: float = 25000.0
    materiality_pct: float = 0.10
    pinned_model_version: str = "jev-1.13.0"
    health_never_fails_rate: float = 0.05
    health_always_fails_rate: float = 0.60
    health_near_band: float = 0.05
    health_near_share: float = 0.35
    health_flag_wrong_min: float = 3
    health_missed_min: float = 3
    health_reviewer_weight: float = 2
    health_preparer_weight: float = 1
    health_stale_closes: int = 2
    health_window_closes: int = 1
    min_sample_size: int = 20
    near_duplicate_threshold: float = 0.85
    arith_tolerance_usd: float = 1000.0
    arith_tolerance_pct: float = 0.01

    @field_validator("materiality_usd", "arith_tolerance_usd", "health_flag_wrong_min",
                     "health_missed_min", "health_reviewer_weight", "health_preparer_weight")
    @classmethod
    def _non_negative(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("materiality_pct", "health_never_fails_rate", "health_always_fails_rate",
                     "health_near_share", "arith_tolerance_pct", "health_near_band",
                     "near_duplicate_threshold", mode="before")
    @classmethod
    def _fraction(cls, value: Any) -> float:
        number = parse_number(value)
        if number is None:
            raise ValueError("required")
        if number > 1.0:  # typed as 10 meaning 10%
            number /= 100.0
        if not 0.0 <= number <= 1.0:
            raise ValueError("must be between 0 and 1")
        return number

    @field_validator("health_stale_closes", "health_window_closes", "min_sample_size", mode="before")
    @classmethod
    def _count(cls, value: Any) -> int:
        number = parse_number(value)
        if number is None or number < 0 or number != int(number):
            raise ValueError("must be a whole number")
        return int(number)

    @classmethod
    def from_sheet_rows(cls, rows: List[Dict[str, Any]]) -> "Settings":
        data: Dict[str, Any] = {}
        for row in rows:
            spec = schema.setting_by_label(str(row.get("Setting", "")).strip())
            if spec is None:
                continue
            value = row.get("Value")
            if spec.kind == "text":
                data[spec.key] = str(value or "").strip()
            elif spec.kind in ("usd", "weight", "count"):
                number = parse_number(value)
                if number is not None:
                    data[spec.key] = number
            else:
                if value not in (None, ""):
                    data[spec.key] = value
        return cls(**data)


class LineIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    line_id: str
    entity: str = ""
    period: str = ""
    basis: str = ""
    account: str = ""
    prior: Optional[float] = None
    current: Optional[float] = None
    variance_usd: Optional[float] = None
    variance_pct: Optional[float] = None
    commentary: str = Field(default="", max_length=20000)
    row: Optional[int] = None

    @field_validator("prior", "current", "variance_usd", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> Optional[float]:
        return parse_number(value)

    @field_validator("variance_pct", mode="before")
    @classmethod
    def _pct(cls, value: Any) -> Optional[float]:
        # The sheet stores a fraction with a % format. A string like "4.5%" is
        # read as 0.045; a bare number is taken as the fraction it already is.
        return parse_number(value)

    @field_validator("commentary", mode="before")
    @classmethod
    def _commentary(cls, value: Any) -> str:
        return "" if value is None else str(value)


class CheckOutcome(BaseModel):
    criterion_id: str
    tier: str  # materiality | commentary | deterministic | heuristic | jev
    result: str  # pass | fail | abstain | skipped | missing | blocked | prompt | error
    likely: bool = False
    probability: Optional[float] = None
    threshold: Optional[float] = None
    calibration_status: Optional[str] = None
    criterion_version: Optional[int] = None
    model_version: Optional[str] = None
    jev_source: Optional[str] = None
    detail: str = ""


class FeedbackItem(BaseModel):
    criterion_id: str
    text: str
    likely: bool


class LineResult(BaseModel):
    line_id: str
    commentary_hash: str
    attempt_number: int
    material: bool
    status: str
    status_label: str
    failed: List[str] = []
    failed_cell: str = ""
    feedback: List[FeedbackItem] = []
    feedback_cell: str = ""
    prompts: List[str] = []
    note: str = ""
    checks: List[CheckOutcome] = []
