"""Configuration from the environment, and the refusals that keep production honest.

Everything is read from environment variables (Railway variables in
production, backend/.env locally). A configuration that would run but quietly
do the wrong thing is refused at startup instead:

  always
    - a Jev model alias (latest, preview, ~...) as the request model;
    - Google auth enabled with no audience to check;
    - live Jev mode with no API key;
    - a canonical pin that is not a versioned jev-x.y.z;
  in production
    - the in-memory store (everything would vanish on restart), unless
      COACH_ALLOW_MEMORY_STORE=1 says that is intended;
    - local (no-credential) auth;
    - a JWKS URL other than Google's;
    - live Jev mode with no snapshot allowlist (thresholds must be tied to
      the snapshot they were measured on).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .auth import GOOGLE_JWKS_URL
from .dsn import problems as dsn_problems
from .jev.client import JevConfigError, validate_request_model

_PIN = re.compile(r"^jev-\d+\.\d+\.\d+$")


def _list(value: Optional[str]) -> Tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def _flag(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    env: str = "development"
    store: str = "memory"
    database_url: str = field(default="", repr=False)
    allow_memory_store: bool = False
    jev_provider: str = "openrouter"
    jev_mode: str = "replay"
    jev_model: str = "typesafe/jev-1.13-20260917"
    jev_pinned_version: str = "jev-1.13.0"
    jev_allowed_snapshots: Tuple[str, ...] = ()
    jev_data_collection: str = "deny"
    openrouter_api_key: str = field(default="", repr=False)
    typesafe_api_key: str = field(default="", repr=False)
    jev_concurrency: int = 8
    auth_modes: Tuple[str, ...] = ("google", "sheet_key")
    google_audiences: Tuple[str, ...] = ()
    google_hosted_domains: Tuple[str, ...] = ()
    google_jwks_url: str = GOOGLE_JWKS_URL
    allow_unlisted: bool = False
    max_body_bytes: int = 1_048_576
    rate_limit_per_minute: int = 60
    lock_ttl_seconds: int = 180
    jev_recordings: Optional[Path] = None  # replay mode: recorded answers (tests, local runs)

    @property
    def production(self) -> bool:
        return self.env == "production"

    def api_key(self) -> str:
        return self.openrouter_api_key if self.jev_provider == "openrouter" else self.typesafe_api_key

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "Config":
        e: Mapping[str, str] = os.environ if environ is None else environ
        return cls(
            env=e.get("COACH_ENV", "development").strip().lower(),
            store=e.get("COACH_STORE", "memory").strip().lower(),
            database_url=e.get("DATABASE_URL", "").strip(),
            allow_memory_store=_flag(e.get("COACH_ALLOW_MEMORY_STORE")),
            jev_provider=e.get("JEV_PROVIDER", "openrouter").strip().lower(),
            jev_mode=e.get("JEV_MODE", "replay").strip().lower(),
            jev_model=e.get("JEV_MODEL", "typesafe/jev-1.13-20260917").strip(),
            jev_pinned_version=e.get("JEV_PINNED_VERSION", "jev-1.13.0").strip(),
            jev_allowed_snapshots=_list(e.get("JEV_ALLOWED_SNAPSHOTS")),
            jev_data_collection=e.get("JEV_DATA_COLLECTION", "deny").strip().lower(),
            openrouter_api_key=e.get("OPENROUTER_API_KEY", "").strip(),
            typesafe_api_key=e.get("TYPESAFE_API_KEY", "").strip(),
            jev_concurrency=int(e.get("JEV_CONCURRENCY", "8")),
            auth_modes=_list(e.get("COACH_AUTH_MODES", "google,sheet_key")),
            google_audiences=_list(e.get("GOOGLE_AUDIENCES")),
            google_hosted_domains=_list(e.get("GOOGLE_HOSTED_DOMAINS")),
            google_jwks_url=e.get("GOOGLE_JWKS_URL", GOOGLE_JWKS_URL).strip(),
            allow_unlisted=_flag(e.get("COACH_ALLOW_UNLISTED")),
            max_body_bytes=int(e.get("COACH_MAX_BODY_BYTES", "1048576")),
            rate_limit_per_minute=int(e.get("COACH_RATE_LIMIT_PER_MINUTE", "60")),
            lock_ttl_seconds=int(e.get("COACH_LOCK_TTL_SECONDS", "180")),
            jev_recordings=Path(e["JEV_RECORDINGS"].strip()) if e.get("JEV_RECORDINGS", "").strip() else None,
        )

    def problems(self) -> List[str]:
        out: List[str] = []
        if self.env not in ("development", "production"):
            out.append("COACH_ENV must be development or production")
        try:
            validate_request_model(self.jev_provider, self.jev_model)
        except JevConfigError as exc:
            out.append(str(exc))
        if not _PIN.match(self.jev_pinned_version):
            out.append("JEV_PINNED_VERSION must look like jev-1.13.0, got {0!r}".format(self.jev_pinned_version))
        if self.jev_mode not in ("replay", "live"):
            out.append("JEV_MODE must be replay or live")
        if self.jev_mode == "live" and not self.api_key():
            out.append("JEV_MODE=live needs {0}".format(
                "OPENROUTER_API_KEY" if self.jev_provider == "openrouter" else "TYPESAFE_API_KEY"))
        if self.jev_data_collection not in ("deny", "allow"):
            out.append("JEV_DATA_COLLECTION must be deny or allow")
        unknown = set(self.auth_modes) - {"google", "sheet_key", "local"}
        if unknown:
            out.append("unknown COACH_AUTH_MODES: {0}".format(sorted(unknown)))
        if not self.auth_modes:
            out.append("COACH_AUTH_MODES is empty: nobody could call the service")
        if "google" in self.auth_modes and not self.google_audiences:
            out.append("Google auth is on but GOOGLE_AUDIENCES is empty; an unchecked audience accepts any "
                       "Google token. Read the value from Coach > Admin > Show connection info.")
        if self.store not in ("memory", "postgres"):
            out.append("COACH_STORE must be memory or postgres")
        if self.store == "postgres" and not self.database_url:
            out.append("COACH_STORE=postgres needs DATABASE_URL")
        elif self.store == "postgres":
            out.extend("DATABASE_URL: " + p for p in dsn_problems(self.database_url))
        if self.production:
            if self.store == "memory" and not self.allow_memory_store:
                out.append("production refuses the in-memory store; set COACH_STORE=postgres "
                           "(or COACH_ALLOW_MEMORY_STORE=1 if losing data on restart is intended)")
            if "local" in self.auth_modes:
                out.append("production refuses local (no-credential) auth")
            if self.google_jwks_url != GOOGLE_JWKS_URL:
                out.append("production only trusts Google's own JWKS URL")
            if self.jev_mode == "live" and not self.jev_allowed_snapshots:
                out.append("production live mode needs JEV_ALLOWED_SNAPSHOTS: thresholds are only valid for "
                           "the snapshot they were calibrated on")
        return out

    def summary(self) -> Dict[str, object]:
        """Safe to show: no secrets, no connection strings."""
        return {
            "env": self.env,
            "store": self.store,
            "auth": list(self.auth_modes),
            "jev": {
                "provider": self.jev_provider,
                "mode": self.jev_mode,
                "request_model": self.jev_model,
                "pinned_version": self.jev_pinned_version,
                "allowed_snapshots": list(self.jev_allowed_snapshots),
                "data_collection": self.jev_data_collection,
            },
        }
