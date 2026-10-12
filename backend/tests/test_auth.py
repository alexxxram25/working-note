from __future__ import annotations

import time

import jwt
import pytest

from coach.auth import (AuthError, Authenticator, GoogleVerifier, Identity, issue_key, require_reviewer,
                        require_sheet)
from coach.store.memory import MemoryStore
from helpers import AUDIENCE, Keys

KEYS = Keys()


@pytest.fixture
def store():
    s = MemoryStore()
    s.upsert_team_member("reviewer@example.com", "reviewer", "Rev")
    s.upsert_team_member("preparer@example.com", "preparer", "Prep")
    s.upsert_team_member("former@example.com", "reviewer", active=False)
    return s


def auth(store, **kw):
    google = GoogleVerifier([AUDIENCE], jwks_client=KEYS.jwks_client(), hosted_domains=kw.pop("domains", ()))
    return Authenticator(store, google, **kw)


def bearer(token):
    return "Bearer " + token


def test_verified_team_member_gets_their_role(store):
    who = auth(store).authenticate(bearer(KEYS.token("Reviewer@Example.com")), None, None)
    assert (who.email, who.role, who.method, who.verified) == ("reviewer@example.com", "reviewer", "google", True)
    assert auth(store).authenticate(bearer(KEYS.token("preparer@example.com")), None, None).role == "preparer"


def test_unlisted_and_inactive_users_are_refused_unless_admitted(store):
    with pytest.raises(AuthError) as raised:
        auth(store).authenticate(bearer(KEYS.token("stranger@example.com")), None, None)
    assert raised.value.status == 403
    with pytest.raises(AuthError):
        auth(store).authenticate(bearer(KEYS.token("former@example.com")), None, None)
    admitted = auth(store, allow_unlisted=True).authenticate(bearer(KEYS.token("stranger@example.com")), None, None)
    assert admitted.role == "preparer" and admitted.verified


def test_token_without_email_is_an_unattributed_preparer(store):
    who = auth(store).authenticate(bearer(KEYS.token(email=None)), None, None)
    assert (who.email, who.role, who.verified) == (None, "preparer", False)


@pytest.mark.parametrize("make", [
    lambda: KEYS.token(aud="someone-elses-client-id"),
    lambda: KEYS.token(iss="https://evil.example.com"),
    lambda: KEYS.token(exp_in=-3600),
    lambda: KEYS.token(verified=False),
    lambda: "not.a.token",
    lambda: "garbage",
])
def test_bad_tokens_are_rejected(store, make):
    with pytest.raises(AuthError):
        auth(store).authenticate(bearer(make()), None, None)


def test_unsigned_token_is_rejected(store):
    now = int(time.time())
    unsigned = jwt.encode({"iss": "https://accounts.google.com", "aud": AUDIENCE, "sub": "1", "iat": now,
                           "exp": now + 600, "email": "reviewer@example.com", "email_verified": True},
                          key=None, algorithm="none")
    with pytest.raises(AuthError):
        auth(store).authenticate(bearer(unsigned), None, None)


def test_hs256_token_signed_with_the_public_key_is_rejected(store):
    """The classic algorithm-confusion attack: the algorithm list is pinned, never read from the token."""
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": AUDIENCE, "sub": "1", "iat": now, "exp": now + 600,
              "email": "reviewer@example.com", "email_verified": True}
    import hmac
    import hashlib
    import base64
    import json

    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    payload = b64(json.dumps(claims).encode())
    signature = b64(hmac.new(KEYS.public_pem, header + b"." + payload, hashlib.sha256).digest())
    forged = (header + b"." + payload + b"." + signature).decode()
    with pytest.raises(AuthError):
        auth(store).authenticate(bearer(forged), None, None)


def test_unknown_signing_key_is_rejected(store):
    google = GoogleVerifier([AUDIENCE], jwks_client=KEYS.jwks_client(fail=True))
    with pytest.raises(AuthError):
        Authenticator(store, google).authenticate(bearer(KEYS.token()), None, None)


def test_workspace_domain_allowlist(store):
    inside = auth(store, domains=["fernhill.example"]).authenticate(
        bearer(KEYS.token("reviewer@example.com", hd="fernhill.example")), None, None)
    assert inside.role == "reviewer"
    with pytest.raises(AuthError) as raised:
        auth(store, domains=["fernhill.example"]).authenticate(bearer(KEYS.token(hd="other.example")), None, None)
    assert raised.value.status == 403


def test_empty_audience_list_is_refused():
    with pytest.raises(ValueError):
        GoogleVerifier([], jwks_client=KEYS.jwks_client())
    with pytest.raises(ValueError):
        GoogleVerifier(["  "], jwks_client=KEYS.jwks_client())


def test_sheet_keys_scope_role_and_revocation(store):
    raw, record = issue_key("sheet-1", "preparer", "Flux sheet", 30)
    store.add_sheet_key(record)
    assert raw.startswith("fcc_k_") and record.key_hash not in raw
    who = auth(store).authenticate(None, raw, "Preparer@Example.com")
    assert (who.role, who.method, who.verified, who.sheet_scope) == ("preparer", "sheet_key", False, "sheet-1")
    assert who.email == "preparer@example.com"  # asserted, stored as unverified
    require_sheet(who, "sheet-1")
    for other in ("sheet-2", "sheet-1/demo-1", "sheet-1x", "*"):
        with pytest.raises(AuthError) as raised:
            require_sheet(who, other)
        assert raised.value.status == 403, other
    store.revoke_sheet_key(record.key_id)
    with pytest.raises(AuthError):
        auth(store).authenticate(None, raw, None)


@pytest.mark.parametrize("sheet_id", ["*", "sheet-*", " ", ""])
def test_a_key_is_never_issued_for_more_than_one_sheet(sheet_id):
    with pytest.raises(ValueError, match="one spreadsheet ID"):
        issue_key(sheet_id, "reviewer", "automation")


def test_a_stored_wildcard_key_opens_no_other_sheet(store):
    """A '*' key left in a database from an older version is held to its literal scope."""
    raw, record = issue_key("sheet-1", "reviewer", "old automation")
    store.add_sheet_key(record._replace(sheet_id="*"))
    who = auth(store).authenticate(None, raw, None)
    for sheet in ("sheet-1", "sheet-2"):
        with pytest.raises(AuthError) as raised:
            require_sheet(who, sheet)
        assert raised.value.status == 403


@pytest.mark.parametrize("raw", ["", "nope", "fcc_k_00000000_wrong"])
def test_unknown_keys_are_rejected(store, raw):
    with pytest.raises(AuthError):
        auth(store).authenticate(None, raw or "x", None)


def test_asserted_email_must_look_like_one(store):
    raw, record = issue_key("sheet-1", "reviewer")
    store.add_sheet_key(record)
    assert auth(store).authenticate(None, raw, "not an email").email is None


def test_modes_and_fallbacks(store):
    with pytest.raises(AuthError):
        Authenticator(store, None).authenticate(bearer(KEYS.token()), None, None)
    with pytest.raises(AuthError):
        Authenticator(store, None, sheet_keys=False).authenticate(None, "fcc_x", None)
    with pytest.raises(AuthError) as raised:
        Authenticator(store, None).authenticate(None, None, None)
    assert raised.value.status == 401
    local = Authenticator(store, None, local=True).authenticate(None, None, None)
    assert local.method == "local" and local.role == "reviewer"
    assert list(auth(store).modes) == ["google", "sheet_key"]
    with pytest.raises(AuthError):
        auth(store).authenticate("Basic abc", None, None)


def test_reviewer_gate():
    require_reviewer(Identity(role="reviewer", method="google", verified=True))
    with pytest.raises(AuthError) as raised:
        require_reviewer(Identity(role="preparer", method="google", verified=True))
    assert raised.value.status == 403
