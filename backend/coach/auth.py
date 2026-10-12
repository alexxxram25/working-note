"""Who is calling, and as what.

Primary: a Google OpenID Connect identity token from ScriptApp.getIdentityToken()
in the sheet's bound script. The token is verified here, not trusted:

  - signature against Google's published keys, RS256 only. The algorithm list
    is pinned and never read from the token's own header; a verifier that
    trusts `alg` can be handed an unsigned token, or an HS256 token "signed"
    with the public key it publishes;
  - issuer is Google;
  - audience is on the allowlist (the script's OAuth client ID). Startup
    refuses an empty allowlist, because PyJWT treats a missing audience as
    "don't check";
  - not expired (60 seconds of clock leeway);
  - if the token carries an email, Google says it is verified; an optional
    Workspace domain allowlist checks the hd claim.

The verified email is looked up in team_roles, so roles are enforced here, not
just hidden in the menu. A verified email that is not on the team list is
refused unless COACH_ALLOW_UNLISTED admits it as a preparer. A token with no
email (Google can withhold it) is a preparer whose overrides save without
attribution.

Fallback: a per-sheet key (X-Coach-Sheet-Key), stored as a SHA-256 hash,
scoped to one sheet ID, capped at a role, rate-limited and revocable. It is
still better than exposing the Jev key, but it cannot say who is calling: any
email sent alongside it is recorded as unverified.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any, Dict, Optional, Sequence

import jwt
from jwt import PyJWKClient
from pydantic import BaseModel

from .store.base import SheetKey, Store

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["accounts.google.com", "https://accounts.google.com"]
ALGORITHMS = ["RS256"]
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KEY_PREFIX = "fcc_"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


class Identity(BaseModel):
    email: Optional[str] = None
    role: str
    method: str  # google | sheet_key | local
    verified: bool
    display_name: Optional[str] = None
    key_id: Optional[str] = None
    sheet_scope: Optional[str] = None
    rate_limit_per_minute: Optional[int] = None

    @property
    def principal(self) -> str:
        if self.method == "sheet_key":
            return "key:" + (self.key_id or "?")
        return "user:" + (self.email or "anonymous")

    @property
    def is_reviewer(self) -> bool:
        return self.role == "reviewer"

    @property
    def attribution(self) -> Optional[str]:
        """The email to stamp on what this caller does, verified or not."""
        return self.email


class GoogleVerifier:
    def __init__(self, audiences: Sequence[str], jwks_url: str = GOOGLE_JWKS_URL,
                 hosted_domains: Sequence[str] = (), leeway: int = 60,
                 jwks_client: Optional[Any] = None) -> None:
        audiences = [a.strip() for a in audiences if a and a.strip()]
        if not audiences:
            raise ValueError("Google auth needs at least one audience (the script's OAuth client ID)")
        self.audiences = audiences
        self.hosted_domains = [d.strip().lower() for d in hosted_domains if d and d.strip()]
        self.leeway = leeway
        # Keys are cached across calls; Google rotates them rarely and a fetch
        # per request would put Google in every request's path.
        self._jwks = jwks_client or PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> Dict[str, Any]:
        if not token or token.count(".") != 2:
            raise AuthError("malformed identity token")
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
        except Exception as exc:  # noqa: BLE001 - network, parse or unknown kid
            raise AuthError("could not resolve the token's signing key: {0}".format(type(exc).__name__))
        try:
            claims = jwt.decode(
                token, key, algorithms=ALGORITHMS, audience=self.audiences, issuer=GOOGLE_ISSUERS,
                leeway=self.leeway, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError("identity token rejected: {0}".format(exc))
        email = claims.get("email")
        if email is not None and claims.get("email_verified") is not True:
            raise AuthError("Google has not verified this account's email")
        if self.hosted_domains and str(claims.get("hd", "")).lower() not in self.hosted_domains:
            raise AuthError("account is outside the allowed Google Workspace domain", status=403)
        return claims


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_key(sheet_id: str, role_ceiling: str, label: Optional[str] = None,
              rate_limit_per_minute: int = 30) -> "tuple[str, SheetKey]":
    """A new sheet key. The raw key is returned once and never stored."""
    if role_ceiling not in ("preparer", "reviewer"):
        raise ValueError("role ceiling must be preparer or reviewer")
    if not sheet_id.strip() or "*" in sheet_id:
        raise ValueError("a sheet key belongs to one spreadsheet ID")
    key_id = "k_" + secrets.token_hex(4)
    raw = "{0}{1}_{2}".format(KEY_PREFIX, key_id, secrets.token_urlsafe(32))
    return raw, SheetKey(key_id, hash_key(raw), sheet_id, role_ceiling, label, rate_limit_per_minute)


class Authenticator:
    def __init__(self, store: Store, google: Optional[GoogleVerifier] = None, sheet_keys: bool = True,
                 allow_unlisted: bool = False, local: bool = False) -> None:
        self.store = store
        self.google = google
        self.sheet_keys = sheet_keys
        self.allow_unlisted = allow_unlisted
        self.local = local

    @property
    def modes(self) -> Sequence[str]:
        out = []
        if self.google:
            out.append("google")
        if self.sheet_keys:
            out.append("sheet_key")
        if self.local:
            out.append("local")
        return out

    def authenticate(self, authorization: Optional[str], sheet_key: Optional[str],
                     asserted_email: Optional[str]) -> Identity:
        if authorization:
            if self.google is None:
                raise AuthError("identity tokens are not enabled on this service")
            parts = authorization.split(None, 1)
            if len(parts) != 2 or parts[0].lower() != "bearer":
                raise AuthError("Authorization header is not a bearer token")
            return self._from_google(self.google.verify(parts[1].strip()))
        if sheet_key:
            if not self.sheet_keys:
                raise AuthError("sheet keys are not enabled on this service")
            return self._from_key(sheet_key.strip(), asserted_email)
        if self.local:
            return Identity(email="local@localhost", role="reviewer", method="local", verified=False)
        raise AuthError("no credentials: send a Google identity token or a sheet key")

    def _from_google(self, claims: Dict[str, Any]) -> Identity:
        email = str(claims["email"]).lower() if claims.get("email") else None
        if email is None:
            return Identity(email=None, role="preparer", method="google", verified=False)
        member = self.store.team_member(email)
        if member is not None and member.active:
            return Identity(email=email, role=member.role, method="google", verified=True,
                            display_name=member.display_name)
        if self.allow_unlisted:
            return Identity(email=email, role="preparer", method="google", verified=True)
        raise AuthError("{0} is not on this team's list; ask a reviewer to add you".format(email), status=403)

    def _from_key(self, raw: str, asserted_email: Optional[str]) -> Identity:
        if not raw.startswith(KEY_PREFIX):
            raise AuthError("malformed sheet key")
        record = self.store.sheet_key(hash_key(raw))
        if record is None or record.revoked_at is not None:
            raise AuthError("unknown or revoked sheet key")
        email = (asserted_email or "").strip().lower() or None
        if email is not None and not _EMAIL.match(email):
            email = None
        self.store.touch_sheet_key(record.key_id)
        return Identity(email=email, role=record.role_ceiling, method="sheet_key", verified=False,
                        key_id=record.key_id, sheet_scope=record.sheet_id,
                        rate_limit_per_minute=record.rate_limit_per_minute)


def require_sheet(identity: Identity, sheet_id: str) -> None:
    """A sheet key works on its own spreadsheet only; a verified identity isn't tied to one."""
    scope = identity.sheet_scope
    if scope is None or scope == sheet_id:
        return
    raise AuthError("this sheet key belongs to a different sheet", status=403)


def require_reviewer(identity: Identity) -> None:
    if not identity.is_reviewer:
        raise AuthError("this action is for reviewers", status=403)
