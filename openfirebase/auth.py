"""Local Auth service.

Email/password sign-up and sign-in that issues and verifies *local* signed
tokens. Tokens are HMAC-signed JWT-like blobs for LOCAL development only -- this
is NOT a real identity provider and must never be used to secure real systems.
Passwords are salted + hashed with PBKDF2-HMAC-SHA256.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any, Dict, Optional

from .storage import BaseStore, MemoryStore

_NS_USERS = "auth::users"
_NS_EMAIL = "auth::email_index"

_PBKDF2_ROUNDS = 120_000


class AuthError(Exception):
    """Raised for any auth failure (bad credentials, expired token, ...)."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class AuthService:
    """Email/password auth with local HMAC tokens."""

    def __init__(self, store: Optional[BaseStore] = None,
                 secret: Optional[str] = None,
                 token_ttl: int = 3600) -> None:
        self._store = store if store is not None else MemoryStore()
        self._secret = (secret or os.environ.get("OPENFIREBASE_SECRET")
                        or "openfirebase-dev-secret").encode("utf-8")
        self._token_ttl = token_ttl

    # ---- password hashing -------------------------------------------------
    def _hash_password(self, password: str, salt: bytes) -> str:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 _PBKDF2_ROUNDS)
        return dk.hex()

    # ---- user management --------------------------------------------------
    def sign_up(self, email: str, password: str,
                display_name: Optional[str] = None) -> Dict[str, Any]:
        email = email.strip().lower()
        if not email or "@" not in email:
            raise AuthError("invalid email")
        if len(password) < 6:
            raise AuthError("password must be at least 6 characters")
        if self._store.get(_NS_EMAIL, email) is not None:
            raise AuthError("email already registered")
        uid = uuid.uuid4().hex
        salt = os.urandom(16)
        record = {
            "uid": uid,
            "email": email,
            "display_name": display_name,
            "salt": salt.hex(),
            "password_hash": self._hash_password(password, salt),
            "created_at": time.time(),
        }
        self._store.set(_NS_USERS, uid, record)
        self._store.set(_NS_EMAIL, email, uid)
        return self._public_user(record)

    def sign_in(self, email: str, password: str) -> Dict[str, Any]:
        email = email.strip().lower()
        uid = self._store.get(_NS_EMAIL, email)
        if uid is None:
            raise AuthError("no such user")
        record = self._store.get(_NS_USERS, uid)
        if record is None:
            raise AuthError("no such user")
        salt = bytes.fromhex(record["salt"])
        if not hmac.compare_digest(self._hash_password(password, salt),
                                   record["password_hash"]):
            raise AuthError("invalid credentials")
        token = self.issue_token(uid)
        return {"user": self._public_user(record), "id_token": token}

    def get_user(self, uid: str) -> Optional[Dict[str, Any]]:
        record = self._store.get(_NS_USERS, uid)
        return self._public_user(record) if record else None

    def get_user_uid(self, email: str) -> Optional[str]:
        return self._store.get(_NS_EMAIL, email.strip().lower())

    def delete_user(self, uid: str) -> bool:
        record = self._store.get(_NS_USERS, uid)
        if not record:
            return False
        self._store.delete(_NS_EMAIL, record["email"])
        return self._store.delete(_NS_USERS, uid)

    # ---- tokens -----------------------------------------------------------
    def issue_token(self, uid: str) -> str:
        header = {"alg": "HS256", "typ": "OFB"}
        now = int(time.time())
        payload = {"sub": uid, "iat": now, "exp": now + self._token_ttl,
                   "iss": "openfirebase"}
        segments = [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":")).encode()),
        ]
        signing_input = ".".join(segments).encode("ascii")
        sig = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        segments.append(_b64url(sig))
        return ".".join(segments)

    def verify_token(self, token: str) -> Dict[str, Any]:
        try:
            h_seg, p_seg, s_seg = token.split(".")
        except ValueError:
            raise AuthError("malformed token")
        signing_input = f"{h_seg}.{p_seg}".encode("ascii")
        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(s_seg)):
            raise AuthError("bad signature")
        payload = json.loads(_b64url_decode(p_seg))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise AuthError("token expired")
        return payload

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _public_user(record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "uid": record["uid"],
            "email": record["email"],
            "display_name": record.get("display_name"),
            "created_at": record.get("created_at"),
        }
