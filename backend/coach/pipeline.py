"""One Coach run over a batch of lines, in strict tier order.

    1. Materiality gate (deterministic). Under both thresholds: skipped
       entirely, logged once as MATERIALITY/skipped, never seen by Jev.
    2. Blank commentary on a material line: Needs commentary, no checks.
    3. Arithmetic (deterministic after parsing): pass, fail or abstain.
    4. Exact duplicate (deterministic) and near duplicate (heuristic) across
       the material lines of the same close.
    5. Jev: the restatement check and the active reviewer criteria, one
       request per line, one noul question per criterion.

A failed arithmetic check or an exact duplicate is an objective failure, so
the line gets that feedback and every Jev question for it is logged as
`blocked`: Jev never grades a line that already failed on something
objective. A near-duplicate flag is heuristic and does not block Jev.

This module decides and assembles; it does not persist. The API stores the
log rows it returns, inside the run lock.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import feedback
from .attempts import PriorAttempt, next_attempt
from .checks import arithmetic, duplicates
from .checks.materiality import Variance, assess, variance_of
from .hashing import commentary_hash, normalize_commentary
from .jev.client import JevError, JevResponse
from .jev.questions import QUESTION_BUILDER_VERSION, build_questions, build_state
from .jev.thresholds import CALIBRATED, UNCALIBRATED, CalibrationIndex, grade
from .line_ids import LineIdError, parse_line_id
from .models import COMMENTARY_ID, MATERIALITY_ID, CheckOutcome, Criterion, LineIn, LineResult, Settings


class NoActiveCriteria(ValueError):
    """Every criterion is inactive. A run with nothing to check is refused, not passed."""


@dataclass
class PipelineConfig:
    criteria: List[Criterion]
    settings: Settings
    calibration: CalibrationIndex
    jev: Any = None  # anything with evaluate(state, questions, session_id) -> JevResponse
    criteria_versions: Dict[str, int] = field(default_factory=dict)
    jev_concurrency: int = 8
    session_id: Optional[str] = None
    # Called at least every heartbeat_every seconds while Jev answers are
    # outstanding, whether or not any arrived: the API renews the write lease
    # here, so a long run does not outlive the lease it took at the start, and
    # a slow answer cannot hold the renewal up. Raising from it abandons the
    # run and cancels the Jev work not yet started.
    heartbeat: Optional[Callable[[], None]] = None
    heartbeat_every: float = 1.0


@dataclass
class PipelineOutput:
    results: List[LineResult]
    log_rows: List[Dict[str, Any]]
    jev_requests: int
    lines_sent_to_jev: List[str]
    rejected: List[Dict[str, str]]


@dataclass
class _Line:
    line: LineIn
    period: str
    variance: Optional[Variance] = None
    material: bool = False
    materiality_reason: str = ""
    blank: bool = False
    hash: str = ""
    attempt: int = 0
    outcomes: List[CheckOutcome] = field(default_factory=list)
    objective_failure: Optional[str] = None
    error: Optional[str] = None


def _log_row(item: _Line, outcome: CheckOutcome) -> Dict[str, Any]:
    return {
        "close_period": item.period,
        "line_id": item.line.line_id,
        "attempt_number": item.attempt,
        "commentary_hash": item.hash,
        "criterion_id": outcome.criterion_id,
        "result": outcome.result,
        "jev_probability": None if outcome.probability is None else round(outcome.probability, 4),
        "criterion_version": outcome.criterion_version,
        "question_builder_version": QUESTION_BUILDER_VERSION if outcome.tier == "jev" else None,
        "model_version": outcome.model_version,
        "calibration_status": outcome.calibration_status,
        "jev_source": outcome.jev_source,
        "threshold": outcome.threshold,
        "detail": {"tier": outcome.tier, "text": outcome.detail} if outcome.detail else {"tier": outcome.tier},
    }


def run(config: PipelineConfig, lines: Sequence[LineIn],
        prior_attempts: Optional[Dict[str, PriorAttempt]] = None) -> PipelineOutput:
    prior_attempts = prior_attempts or {}
    settings = config.settings
    active = [c for c in config.criteria if c.active]
    if not active:
        raise NoActiveCriteria("no active criteria: nothing would be checked, so no line can be graded")
    by_id = {c.id: c for c in active}
    jev_criteria = [c for c in active if c.kind == "jev"]
    version = config.criteria_versions.get

    items: List[_Line] = []
    rejected: List[Dict[str, str]] = []
    # A Line ID that appears twice is rejected in every occurrence, before any
    # check runs. Grading the first and dropping the rest would attach one row's
    # verdict to another row's text, and the Log would carry it under that ID.
    rows_by_id: Dict[str, List[str]] = {}
    for line in lines:
        rows_by_id.setdefault(line.line_id, []).append(str(line.row or "?"))
    for line in lines:
        try:
            parts = parse_line_id(line.line_id)
        except LineIdError as exc:
            rejected.append({"line_id": line.line_id, "row": str(line.row or ""), "error": str(exc)})
            continue
        if len(rows_by_id[line.line_id]) > 1:
            rejected.append({"line_id": line.line_id, "row": str(line.row or ""),
                             "error": "Line ID appears {0} times in this run (rows {1}); none was graded".format(
                                 len(rows_by_id[line.line_id]), ", ".join(rows_by_id[line.line_id]))})
            continue
        item = _Line(line=line, period=parts.period)
        try:
            item.variance = variance_of(line.prior, line.current, line.variance_usd, line.variance_pct)
        except ValueError as exc:
            item.error = "cannot read the variance: {0}".format(exc)
            items.append(item)
            continue
        verdict = assess(item.variance, settings.materiality_usd, settings.materiality_pct)
        item.material, item.materiality_reason = verdict.material, verdict.reason
        item.blank = not normalize_commentary(line.commentary)
        item.hash = commentary_hash(line.commentary)
        item.attempt = next_attempt(prior_attempts.get(line.line_id), item.hash, item.blank)
        items.append(item)

    gradeable = [i for i in items if i.error is None and i.material and not i.blank]

    # Tier 1: materiality and blank commentary.
    for item in items:
        if item.error is not None:
            continue
        if not item.material:
            item.outcomes.append(CheckOutcome(criterion_id=MATERIALITY_ID, tier="materiality", result="skipped",
                                              detail=item.materiality_reason))
        elif item.blank:
            item.outcomes.append(CheckOutcome(criterion_id=COMMENTARY_ID, tier="commentary", result="missing"))

    # Tier 2: arithmetic.
    if "ARITH" in by_id:
        for item in gradeable:
            result = arithmetic.check(
                item.line.commentary, item.variance.usd, item.variance.pct, item.line.prior, item.line.current,
                settings.arith_tolerance_usd, settings.arith_tolerance_pct)
            item.outcomes.append(CheckOutcome(
                criterion_id="ARITH", tier="deterministic", result=result.result,
                criterion_version=version("ARITH"), detail=result.detail,
                calibration_status="Deterministic"))
            if result.result == arithmetic.FAIL:
                item.objective_failure = "ARITH"

    # Tier 3: duplicates, within each close.
    if "DUP" in by_id or "NEAR" in by_id:
        by_period: Dict[str, List[_Line]] = {}
        for item in gradeable:
            by_period.setdefault(item.period, []).append(item)
        for group in by_period.values():
            found = duplicates.find([(i.line.line_id, i.line.commentary) for i in group],
                                    settings.near_duplicate_threshold)
            for item in group:
                dup = found[item.line.line_id]
                if "DUP" in by_id:
                    item.outcomes.append(CheckOutcome(
                        criterion_id="DUP", tier="deterministic", result="fail" if dup.exact else "pass",
                        criterion_version=version("DUP"), calibration_status="Deterministic",
                        detail="identical to {0}".format(", ".join(dup.exact_with)) if dup.exact else ""))
                    if dup.exact:
                        item.objective_failure = item.objective_failure or "DUP"
                if "NEAR" in by_id:
                    item.outcomes.append(CheckOutcome(
                        criterion_id="NEAR", tier="heuristic", result="fail" if dup.near else "pass",
                        likely=True, criterion_version=version("NEAR"), calibration_status="Heuristic",
                        detail="; ".join("{0:.2f} similar to {1}".format(s, lid) for lid, s in dup.near_with)))

    # Tier 4: Jev, only for lines with no objective failure.
    to_jev = [i for i in gradeable if i.objective_failure is None and jev_criteria]
    for item in gradeable:
        if item.objective_failure is not None:
            for criterion in jev_criteria:
                item.outcomes.append(CheckOutcome(
                    criterion_id=criterion.id, tier="jev", result="blocked",
                    criterion_version=version(criterion.id),
                    detail="not sent to Jev: {0} failed".format(item.objective_failure)))

    askable = [c for c in jev_criteria if c.passes_when.strip()]
    unaskable = [c for c in jev_criteria if not c.passes_when.strip()]
    questions = build_questions(askable)

    def ask(item: _Line) -> Optional[JevResponse]:
        if config.jev is None:
            raise JevError("Jev is not configured for this service")
        state = build_state(item.line, item.variance.usd, item.variance.pct)
        return config.jev.evaluate(state, questions, config.session_id)

    responses: Dict[str, Any] = {}
    if to_jev and askable:
        pool = ThreadPoolExecutor(max_workers=max(1, config.jev_concurrency))
        try:
            futures = {pool.submit(ask, item): item.line.line_id for item in to_jev}
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=config.heartbeat_every, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        responses[futures[future]] = future.result()
                    except JevError as exc:
                        responses[futures[future]] = exc
                if config.heartbeat is not None:
                    config.heartbeat()
        finally:
            # On the way out of a failed run, nothing queued is worth asking.
            pool.shutdown(wait=True, cancel_futures=True)

    jev_requests = 0
    for item in to_jev:
        response = responses.get(item.line.line_id)
        for criterion in unaskable:
            item.outcomes.append(CheckOutcome(
                criterion_id=criterion.id, tier="jev", result="error", criterion_version=version(criterion.id),
                detail="no pass condition written for this criterion"))
        if not askable:
            continue
        if isinstance(response, JevError) or response is None:
            message = str(response) if response is not None else "no response"
            for criterion in askable:
                item.outcomes.append(CheckOutcome(
                    criterion_id=criterion.id, tier="jev", result="error",
                    criterion_version=version(criterion.id), detail=message))
            continue
        jev_requests += response.requests
        for criterion in askable:
            answer = response.answers[criterion.id]
            calibrated, _ = config.calibration.is_calibrated(criterion, answer.model)
            verdict = grade(answer.probability, criterion.threshold, calibrated)
            item.outcomes.append(CheckOutcome(
                criterion_id=criterion.id, tier="jev", result=verdict.result, likely=True,
                probability=answer.probability, threshold=verdict.threshold,
                calibration_status=CALIBRATED if calibrated else UNCALIBRATED,
                criterion_version=version(criterion.id), model_version=answer.model,
                jev_source=answer.source))

    results: List[LineResult] = []
    log_rows: List[Dict[str, Any]] = []
    for item in items:
        if item.error is not None:
            results.append(LineResult(
                line_id=item.line.line_id, commentary_hash=commentary_hash(item.line.commentary),
                attempt_number=0, material=False, status=feedback.ERROR,
                status_label=feedback.STATUS_LABELS[feedback.ERROR], note=item.error))
            continue
        failed, failed_cell, items_fb, cell, prompts = feedback.assemble(item.outcomes, config.criteria)
        status = feedback.status_of(item.outcomes, item.material, item.blank)
        results.append(LineResult(
            line_id=item.line.line_id,
            commentary_hash=item.hash,
            attempt_number=item.attempt,
            material=item.material,
            status=status,
            status_label=feedback.STATUS_LABELS[status],
            failed=failed,
            failed_cell=failed_cell,
            feedback=items_fb,
            feedback_cell=cell,
            prompts=prompts,
            note=feedback.note(item.outcomes, config.criteria, item.attempt),
            checks=item.outcomes,
        ))
        log_rows.extend(_log_row(item, o) for o in item.outcomes)

    return PipelineOutput(
        results=results,
        log_rows=log_rows,
        jev_requests=jev_requests,
        lines_sent_to_jev=[i.line.line_id for i in to_jev if askable],
        rejected=rejected,
    )
