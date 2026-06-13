"""Local Auth service — deep implementation.

Email/password sign-up and sign-in that issues and verifies *local* signed
tokens. Tokens are HMAC-signed JWT-like blobs for LOCAL development only -- this
is NOT a real identity provider and must never be used to secure real systems.
Passwords are salted + hashed with PBKDF2-HMAC-SHA256.

New in messaging+compute pass
------------------------------
* Custom-token mint (``mint_custom_token``) + verify
* ID-token verify with custom claims passthrough
* ``update_user`` — display_name, email, password, disabled, custom_claims
* ``list_users`` — paginated listing
* Password-reset OTP flow (generate/confirm/apply)
* Email-verification OTP flow (generate/confirm)
* Provider sign-in stubs (google, github, …)
* Custom claims embedded in tokens and surfaced on verify
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional

from .storage import BaseStore, MemoryStore

_NS_USERS = "auth::users"
_NS_EMAIL = "auth::email_index"
_NS_RESET = "auth::pw_reset"          # key = token  →  {uid, expires}
_NS_VERIFY = "auth::email_verify"     # key = token  →  {uid, expires}
_NS_PROVIDERS = "auth::provider_idx"  # key = "provider:provider_uid"  →  uid

_PBKDF2_ROUNDS = 120_000
_OTP_TTL = 3600  # 1 hour for password-reset / email-verification links


class AuthError(Exception):
    """Raised for any auth failure (bad credentials, expired token, ...)."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _otp() -> str:
    """Return a URL-safe 32-byte random token (hex)."""
    return secrets.token_hex(32)


class AuthService:
    """Email/password auth with local HMAC tokens.

    Implements sign-up/sign-in/verify, custom-token mint, user CRUD,
    listUsers, password-reset OTP, email-verification OTP, provider
    sign-in stubs, and custom claims.
    """

    def __init__(self, store: Optional[BaseStore] = None,
                 secret: Optional[str] = None,
                 token_ttl: int = 3600) -> None:
        self._store = store if store is not None else MemoryStore()
        self._secret = (secret or os.environ.get("OPENFIREBASE_SECRET")
                        or "openfirebase-dev-secret").encode("utf-8")
        self._token_ttl = token_ttl

    # ---- password hashing ---------------------------------------------------
    def _hash_password(self, password: str, salt: bytes) -> str:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 _PBKDF2_ROUNDS)
        return dk.hex()

    # ---- user management ----------------------------------------------------
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
            "email_verified": False,
            "disabled": False,
            "custom_claims": {},
            "provider_data": [],
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
        if record.get("disabled"):
            raise AuthError("user account is disabled")
        salt = bytes.fromhex(record["salt"])
        if not hmac.compare_digest(self._hash_password(password, salt),
                                   record["password_hash"]):
            raise AuthError("invalid credentials")
        token = self.issue_token(uid)
        return {"user": self._public_user(record), "id_token": token}

    def get_user(self, uid: str) -> Optional[Dict[str, Any]]:
        record = self._store.get(_NS_USERS, uid)
        return self._public_user(record) if record else None

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        uid = self._store.get(_NS_EMAIL, email.strip().lower())
        if uid is None:
            return None
        return self.get_user(uid)

    def get_user_uid(self, email: str) -> Optional[str]:
        return self._store.get(_NS_EMAIL, email.strip().lower())

    def delete_user(self, uid: str) -> bool:
        record = self._store.get(_NS_USERS, uid)
        if not record:
            return False
        self._store.delete(_NS_EMAIL, record["email"])
        # remove provider mappings
        for pd in record.get("provider_data", []):
            pkey = f"{pd['provider_id']}:{pd['uid']}"
            self._store.delete(_NS_PROVIDERS, pkey)
        return self._store.delete(_NS_USERS, uid)

    def update_user(self, uid: str, **kwargs) -> Dict[str, Any]:
        """Update user record fields.

        Supported kwargs: display_name, email, password, disabled,
        email_verified, custom_claims.
        Returns the updated public user dict.
        Raises AuthError if user not found or new email already taken.
        """
        record = self._store.get(_NS_USERS, uid)
        if record is None:
            raise AuthError(f"no user with uid {uid!r}")
        record = dict(record)

        if "email" in kwargs:
            new_email = kwargs["email"].strip().lower()
            if new_email != record["email"]:
                if self._store.get(_NS_EMAIL, new_email) is not None:
                    raise AuthError("email already registered")
                self._store.delete(_NS_EMAIL, record["email"])
                self._store.set(_NS_EMAIL, new_email, uid)
                record["email"] = new_email

        if "password" in kwargs:
            pw = kwargs["password"]
            if len(pw) < 6:
                raise AuthError("password must be at least 6 characters")
            salt = os.urandom(16)
            record["salt"] = salt.hex()
            record["password_hash"] = self._hash_password(pw, salt)

        if "display_name" in kwargs:
            record["display_name"] = kwargs["display_name"]
        if "disabled" in kwargs:
            record["disabled"] = bool(kwargs["disabled"])
        if "email_verified" in kwargs:
            record["email_verified"] = bool(kwargs["email_verified"])
        if "custom_claims" in kwargs:
            if not isinstance(kwargs["custom_claims"], dict):
                raise AuthError("custom_claims must be a dict")
            record["custom_claims"] = kwargs["custom_claims"]

        self._store.set(_NS_USERS, uid, record)
        return self._public_user(record)

    def list_users(self, page_size: int = 100,
                   page_token: Optional[str] = None) -> Dict[str, Any]:
        """Return up to ``page_size`` users.

        ``page_token`` is a uid — list starts *after* that uid.
        Returns ``{"users": [...], "next_page_token": str|None}``.
        """
        all_users = [(k, v) for k, v in self._store.items(_NS_USERS)]
        # stable order by created_at then uid
        all_users.sort(key=lambda kv: (kv[1].get("created_at", 0), kv[0]))
        if page_token:
            uids = [k for k, _ in all_users]
            if page_token in uids:
                idx = uids.index(page_token)
                all_users = all_users[idx + 1:]
        page = all_users[:page_size]
        remaining = all_users[page_size:]
        next_token = page[-1][0] if remaining else None
        return {
            "users": [self._public_user(v) for _, v in page],
            "next_page_token": next_token,
        }

    # ---- custom-token mint ---------------------------------------------------
    def mint_custom_token(self, uid: str,
                          custom_claims: Optional[Dict[str, Any]] = None,
                          ttl: Optional[int] = None) -> str:
        """Issue a custom token for ``uid`` with optional ``custom_claims``.

        The token is a signed local token like an id-token but with
        ``"typ": "CT"`` and claims embedded in the payload.
        """
        header = {"alg": "HS256", "typ": "CT"}
        now = int(time.time())
        payload: Dict[str, Any] = {
            "sub": uid,
            "iat": now,
            "exp": now + (ttl if ttl is not None else self._token_ttl),
            "iss": "openfirebase",
            "custom_claims": custom_claims or {},
        }
        segments = [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":")).encode()),
        ]
        signing_input = ".".join(segments).encode("ascii")
        sig = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        segments.append(_b64url(sig))
        return ".".join(segments)

    def verify_custom_token(self, token: str) -> Dict[str, Any]:
        """Verify a custom token; returns its payload (includes custom_claims)."""
        payload = self._verify_signature(token)
        header = json.loads(_b64url_decode(token.split(".")[0]))
        if header.get("typ") != "CT":
            raise AuthError("not a custom token")
        return payload

    # ---- id-token issue / verify --------------------------------------------
    def issue_token(self, uid: str) -> str:
        record = self._store.get(_NS_USERS, uid)
        custom_claims: Dict[str, Any] = {}
        if record:
            custom_claims = record.get("custom_claims") or {}
        header = {"alg": "HS256", "typ": "OFB"}
        now = int(time.time())
        payload: Dict[str, Any] = {
            "sub": uid,
            "iat": now,
            "exp": now + self._token_ttl,
            "iss": "openfirebase",
        }
        if custom_claims:
            payload["claims"] = custom_claims
        segments = [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":")).encode()),
        ]
        signing_input = ".".join(segments).encode("ascii")
        sig = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        segments.append(_b64url(sig))
        return ".".join(segments)

    def verify_token(self, token: str) -> Dict[str, Any]:
        return self._verify_signature(token)

    def _verify_signature(self, token: str) -> Dict[str, Any]:
        try:
            h_seg, p_seg, s_seg = token.split(".")
        except ValueError:
            raise AuthError("malformed token")
        try:
            signing_input = f"{h_seg}.{p_seg}".encode("ascii")
            expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
            sig_bytes = _b64url_decode(s_seg)
        except Exception:
            raise AuthError("malformed token")
        if not hmac.compare_digest(expected, sig_bytes):
            raise AuthError("bad signature")
        try:
            payload = json.loads(_b64url_decode(p_seg))
        except Exception:
            raise AuthError("malformed token payload")
        if int(payload.get("exp", 0)) < int(time.time()):
            raise AuthError("token expired")
        return payload

    # ---- password-reset OTP flow --------------------------------------------
    def generate_password_reset_token(self, email: str) -> str:
        """Generate a password-reset OTP for the user with ``email``.

        Returns the OTP (a hex string). In production this would be e-mailed;
        here it is returned directly for local dev / testing.
        """
        email = email.strip().lower()
        uid = self._store.get(_NS_EMAIL, email)
        if uid is None:
            raise AuthError("no such user")
        token = _otp()
        self._store.set(_NS_RESET, token, {
            "uid": uid,
            "expires": time.time() + _OTP_TTL,
        })
        return token

    def confirm_password_reset(self, reset_token: str, new_password: str) -> None:
        """Apply the password reset.  Consumes the OTP."""
        rec = self._store.get(_NS_RESET, reset_token)
        if rec is None:
            raise AuthError("invalid or expired reset token")
        if time.time() > rec["expires"]:
            self._store.delete(_NS_RESET, reset_token)
            raise AuthError("reset token has expired")
        self._store.delete(_NS_RESET, reset_token)
        self.update_user(rec["uid"], password=new_password)

    # ---- email-verification OTP flow ----------------------------------------
    def generate_email_verification_token(self, uid: str) -> str:
        """Generate an email-verification OTP for ``uid``.

        Returns the OTP. In production this would be e-mailed.
        """
        record = self._store.get(_NS_USERS, uid)
        if record is None:
            raise AuthError(f"no user with uid {uid!r}")
        token = _otp()
        self._store.set(_NS_VERIFY, token, {
            "uid": uid,
            "expires": time.time() + _OTP_TTL,
        })
        return token

    def confirm_email_verification(self, verify_token: str) -> Dict[str, Any]:
        """Mark the user's email as verified.  Consumes the OTP."""
        rec = self._store.get(_NS_VERIFY, verify_token)
        if rec is None:
            raise AuthError("invalid or expired verification token")
        if time.time() > rec["expires"]:
            self._store.delete(_NS_VERIFY, verify_token)
            raise AuthError("verification token has expired")
        self._store.delete(_NS_VERIFY, verify_token)
        return self.update_user(rec["uid"], email_verified=True)

    # ---- provider sign-in stubs ---------------------------------------------
    # These simulate "link a provider credential to a uid" without doing a real
    # OAuth round-trip.  The caller provides a provider_id + provider_uid and
    # we either create a new user or return the existing one.

    SUPPORTED_PROVIDERS = {"google.com", "github.com", "facebook.com",
                           "twitter.com", "apple.com", "microsoft.com",
                           "anonymous"}

    def sign_in_with_provider(self, provider_id: str, provider_uid: str,
                               email: Optional[str] = None,
                               display_name: Optional[str] = None) -> Dict[str, Any]:
        """Stub: link/sign-in a provider credential.

        Looks up an existing user by provider_id + provider_uid.  If not
        found, creates a new user (email-less or with the supplied email).
        Returns ``{"user": ..., "id_token": ...}``.
        """
        if provider_id not in self.SUPPORTED_PROVIDERS:
            raise AuthError(f"unsupported provider: {provider_id!r}")
        pkey = f"{provider_id}:{provider_uid}"
        uid = self._store.get(_NS_PROVIDERS, pkey)
        if uid is None:
            # create new user
            uid = uuid.uuid4().hex
            norm_email = email.strip().lower() if email else None
            if norm_email and self._store.get(_NS_EMAIL, norm_email) is not None:
                # email already taken — link the provider to the existing user
                uid = self._store.get(_NS_EMAIL, norm_email)
                record = self._store.get(_NS_USERS, uid)
                record = dict(record)
                pdata = record.get("provider_data", [])
                if not any(p["uid"] == provider_uid and p["provider_id"] == provider_id
                           for p in pdata):
                    pdata.append({"provider_id": provider_id, "uid": provider_uid})
                    record["provider_data"] = pdata
                    self._store.set(_NS_USERS, uid, record)
                self._store.set(_NS_PROVIDERS, pkey, uid)
                token = self.issue_token(uid)
                return {"user": self._public_user(record), "id_token": token}
            # brand new user
            record = {
                "uid": uid,
                "email": norm_email,
                "display_name": display_name,
                "salt": "",
                "password_hash": "",
                "created_at": time.time(),
                "email_verified": bool(norm_email),
                "disabled": False,
                "custom_claims": {},
                "provider_data": [{"provider_id": provider_id, "uid": provider_uid}],
            }
            self._store.set(_NS_USERS, uid, record)
            if norm_email:
                self._store.set(_NS_EMAIL, norm_email, uid)
            self._store.set(_NS_PROVIDERS, pkey, uid)
        else:
            record = self._store.get(_NS_USERS, uid)
            if record is None:
                raise AuthError("provider-linked user record is missing")
            if record.get("disabled"):
                raise AuthError("user account is disabled")

        token = self.issue_token(uid)
        return {"user": self._public_user(record), "id_token": token}

    # ---- custom claims management -------------------------------------------
    def set_custom_claims(self, uid: str, claims: Dict[str, Any]) -> Dict[str, Any]:
        """Overwrite the custom claims for ``uid``.  Returns updated public user."""
        return self.update_user(uid, custom_claims=claims)

    # ---- helpers ------------------------------------------------------------
    @staticmethod
    def _public_user(record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "uid": record["uid"],
            "email": record.get("email"),
            "display_name": record.get("display_name"),
            "created_at": record.get("created_at"),
            "email_verified": record.get("email_verified", False),
            "disabled": record.get("disabled", False),
            "custom_claims": record.get("custom_claims", {}),
            "provider_data": record.get("provider_data", []),
        }
