"""Jev clients: live (OpenRouter or TypeSafe direct) and the shared response type.

Pinning. The request model is a pinned version, never an alias: anything
containing "latest" or "preview", or starting with "~", is refused when the
client is built. Responses report the snapshot that actually answered; it must
belong to the pinned version family and, when an allowlist is configured, be
on it. A response from any other snapshot raises JevModelDrift and the line is
not graded, because its thresholds were calibrated on a different model.

Retries. 429, 500, 502, 503, 524 and 529 are retried with exponential backoff
(honouring Retry-After, capped); 400, 401, 402, 403, 404, 413 and 422 are not,
because retrying a bad key or a bad request only repeats the failure.

Privacy. On OpenRouter the request asks for no provider fallbacks and, by
default, providers that do not collect data. Commentary text and the row's
figures leave the service in the request body; nothing else does (no emails,
no entity, no Line ID). The API key is never logged or echoed in errors.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Callable, Dict, Iterable, NamedTuple, Optional, Sequence

import httpx

PROVIDERS = {
    "openrouter": {
        "url": "https://openrouter.ai/api/alpha/decisions",
        "model_pattern": re.compile(r"^typesafe/jev-\d+\.\d+(?:\.\d+)?(?:-\d{8})?$"),
    },
    "typesafe": {
        "url": "https://api.typesafe.ai/v1/systemone",
        "model_pattern": re.compile(r"^jev-\d+\.\d+\.\d+$"),
    },
}

_ALIAS = re.compile(r"latest|preview", re.IGNORECASE)
RETRIABLE = {429, 500, 502, 503, 524, 529}
_MAX_RETRY_AFTER = 20.0


class JevError(Exception):
    """A Jev call failed. `retriable` says whether trying again could help."""

    def __init__(self, message: str, status: Optional[int] = None, retriable: bool = False):
        super().__init__(message)
        self.status = status
        self.retriable = retriable


class JevConfigError(JevError):
    pass


class JevModelDrift(JevError):
    pass


class JevBadResponse(JevError):
    pass


class JevReplayMiss(JevError):
    pass


class JevAnswer(NamedTuple):
    probability: float
    model: str
    source: str  # live | replay | cache


class JevResponse(NamedTuple):
    answers: Dict[str, JevAnswer]
    usage: Dict[str, Any]
    requests: int  # live HTTP requests actually made (0 for replay and cache hits)


def family_of(model: str) -> str:
    """'typesafe/jev-1.13-20260917' -> 'typesafe/jev-1.13'; 'jev-1.13.0' -> 'jev-1.13'."""
    match = re.match(r"^((?:typesafe/)?jev-\d+\.\d+)", model or "")
    if not match:
        raise JevConfigError("not a Jev model id: {0!r}".format(model))
    return match.group(1)


def validate_request_model(provider: str, model: str) -> str:
    if provider not in PROVIDERS:
        raise JevConfigError("unknown Jev provider {0!r}; use one of {1}".format(provider, sorted(PROVIDERS)))
    model = (model or "").strip()
    if not model or model.startswith("~") or _ALIAS.search(model):
        raise JevConfigError(
            "refusing Jev model {0!r}: pin a version, never an alias like latest or preview".format(model))
    if not PROVIDERS[provider]["model_pattern"].match(model):
        raise JevConfigError("{0!r} is not a pinned {1} model id".format(model, provider))
    return model


def check_snapshot(served: str, request_model: str, allowed: Sequence[str]) -> str:
    if not isinstance(served, str) or not served:
        raise JevBadResponse("response has no model field")
    if family_of(served) != family_of(request_model):
        raise JevModelDrift("Jev answered with {0}, outside the pinned {1}".format(served, family_of(request_model)))
    if allowed and served not in allowed:
        raise JevModelDrift(
            "Jev answered with snapshot {0}, which is not one the thresholds were calibrated on ({1})".format(
                served, ", ".join(allowed)))
    return served


def parse_response(data: Any, questions: Iterable[str], request_model: str, allowed: Sequence[str],
                   source: str) -> Dict[str, JevAnswer]:
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise JevBadResponse("response is missing answers")
    served = check_snapshot(data.get("model"), request_model, allowed)
    answers = {}
    for qid in questions:
        answer = data["answers"].get(qid)
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            raise JevBadResponse("no noul answer for {0}".format(qid))
        value = answer.get("noul")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            raise JevBadResponse("noul for {0} is not a probability: {1!r}".format(qid, value))
        answers[qid] = JevAnswer(float(value), served, source)
    return answers


class LiveJevClient:
    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        allowed_snapshots: Sequence[str] = (),
        url: Optional[str] = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        data_collection: str = "deny",
        http: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.provider = provider
        self.model = validate_request_model(provider, model)
        if not api_key:
            raise JevConfigError("no API key for the {0} Jev provider".format(provider))
        if data_collection not in ("deny", "allow"):
            raise JevConfigError("data_collection must be deny or allow")
        self._key = api_key
        self.allowed = tuple(allowed_snapshots)
        self.url = url or PROVIDERS[provider]["url"]
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.data_collection = data_collection
        self._http = http or httpx.Client(timeout=timeout)
        self._sleep = sleep
        self._rng = rng or random.Random()

    def __repr__(self) -> str:  # never show the key
        return "LiveJevClient(provider={0!r}, model={1!r})".format(self.provider, self.model)

    def body(self, state: Dict[str, Any], questions: Dict[str, Any], session_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": self.model, "state": state, "questions": questions}
        if self.provider == "openrouter":
            body["provider"] = {"allow_fallbacks": False, "data_collection": self.data_collection}
            if session_id:
                body["session_id"] = session_id[:256]
        return body

    def _delay(self, attempt: int, response: Optional[httpx.Response]) -> float:
        if response is not None:
            header = response.headers.get("retry-after")
            if header:
                try:
                    return min(float(header), _MAX_RETRY_AFTER)
                except ValueError:
                    pass
        return self.backoff * (2 ** attempt) * (1 + 0.25 * self._rng.random())

    def evaluate(self, state: Dict[str, Any], questions: Dict[str, Any],
                 session_id: Optional[str] = None) -> JevResponse:
        body = self.body(state, questions, session_id)
        headers = {"Authorization": "Bearer " + self._key, "Content-Type": "application/json"}
        requests = 0
        last_error: Optional[JevError] = None
        for attempt in range(self.max_retries + 1):
            response = None
            try:
                requests += 1
                response = self._http.post(self.url, json=body, headers=headers, timeout=self.timeout)
            except httpx.TransportError as exc:
                last_error = JevError("could not reach Jev: {0}".format(type(exc).__name__), retriable=True)
            else:
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        raise JevBadResponse("Jev returned non-JSON", status=200)
                    answers = parse_response(data, questions, self.model, self.allowed, "live")
                    usage = dict(data.get("usage") or {})
                    usage["requests"] = requests
                    return JevResponse(answers, usage, requests)
                last_error = JevError(
                    "Jev returned {0}: {1}".format(response.status_code, _error_message(response)),
                    status=response.status_code, retriable=response.status_code in RETRIABLE)
            if not last_error.retriable or attempt == self.max_retries:
                break
            self._sleep(self._delay(attempt, response))
        raise last_error  # type: ignore[misc]


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message", ""))[:200]
    if isinstance(data, dict) and "detail" in data:
        return str(data["detail"])[:200]
    return str(data)[:200]
