"""Test helpers: a fake Jev, signing keys for Google-style tokens, and an app factory."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Dict, List, Optional

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from coach.jev.client import JevAnswer, JevError, JevResponse

SNAP = "typesafe/jev-1.13-20260917"
AUDIENCE = "1234567890-test.apps.googleusercontent.com"


class FakeJev:
    """Answers from a table keyed by commentary text; records every call."""

    def __init__(self, table: Optional[Dict[str, Dict[str, float]]] = None, default: float = 0.99,
                 fail_on: str = "", explode_on: str = ""):
        self.table = table or {}
        self.default = default
        self.fail_on = fail_on
        self.explode_on = explode_on
        self.calls: List[Dict] = []

    def evaluate(self, state, questions, session_id=None):
        self.calls.append({"commentary": state["commentary"], "questions": sorted(questions)})
        if self.explode_on and self.explode_on in state["commentary"]:
            raise RuntimeError("unexpected bug")
        if self.fail_on and self.fail_on in state["commentary"]:
            raise JevError("Jev returned 529: overloaded", status=529, retriable=True)
        scores = self.table.get(state["commentary"], {})
        return JevResponse({q: JevAnswer(scores.get(q, self.default), SNAP, "live") for q in questions},
                           {"input_tokens": 100}, 1)


class Keys:
    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public = self.private.public_key()
        self.public_pem = self.public.public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)

    def token(self, email: Optional[str] = "reviewer@example.com", aud: str = AUDIENCE,
              iss: str = "https://accounts.google.com", exp_in: int = 3600, verified: bool = True,
              hd: Optional[str] = None, kid: str = "k1", **extra) -> str:
        now = int(time.time())
        claims = {"iss": iss, "aud": aud, "sub": "1001", "iat": now, "exp": now + exp_in}
        if email is not None:
            claims["email"] = email
            claims["email_verified"] = verified
        if hd:
            claims["hd"] = hd
        claims.update(extra)
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": kid})

    def jwks_client(self, fail: bool = False):
        public = self.public

        class _Client:
            def get_signing_key_from_jwt(self, token):
                if fail:
                    raise jwt.PyJWKClientError("kid not found")
                return SimpleNamespace(key=public)

        return _Client()
