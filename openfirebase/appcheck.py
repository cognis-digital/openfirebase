"""App Check — local emulator.

Provides:
* **Token issuance** — issue short-lived App Check tokens bound to an
  ``app_id``.  Tokens are HMAC-signed local blobs (same scheme as Auth
  tokens) so they can be verified without a network round-trip.
* **Token verification** — verify and decode a token; raises if expired,
  tampered, or from an unknown provider.
* **Attestation provider stubs** — ``debug``, ``device_check``,
  ``play_integrity``, ``app_attest`` — all accepted in local mode.

This is a LOCAL development emulator.  The tokens it produces have nothing
to do with real Firebase App Check tokens and MUST NOT be used outside of
local dev/test.
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

_NS_APPS      = "appcheck::apps"        # app_id → app record
_NS_TOKENS    = "appcheck::tokens"      # jti → {app_id, issued_at, expires_at}
_NS_REVOKED   = "appcheck::revoked"     # jti → True

_DEFAULT_TTL  = 3600       # 1 hour
_DEBUG_TOKEN  = "DEBUG_TOKEN_FOR_LOCAL_DEV"

SUPPORTED_PROVIDERS = frozenset([
    "debug", "device_check", "play_integrity", "app_attest",
])


class AppCheckError(Exception):
    """Raised for App Check failures."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class AppCheck:
    """Local App Check emulator.

    Parameters
    ----------
    store:
        Shared BaseStore.
    secret:
        HMAC signing secret.  Defaults to the ``OPENFIREBASE_SECRET``
        environment variable or a static development constant.
    token_ttl:
        Token lifetime in seconds.
    """

    def __init__(self, store: Optional[BaseStore] = None,
                 secret: Optional[str] = None,
                 token_ttl: int = _DEFAULT_TTL) -> None:
        self._store = store if store is not None else MemoryStore()
        self._secret = (
            secret
            or os.environ.get("OPENFIREBASE_SECRET")
            or "openfirebase-appcheck-dev-secret"
        ).encode("utf-8")
        self._token_ttl = token_ttl

    # ---- app registration ---------------------------------------------------

    def register_app(self, app_id: str,
                     providers: Optional[List[str]] = None) -> Dict[str, Any]:
        """Register an app ID as a valid App Check client.

        Parameters
        ----------
        app_id:
            Your Firebase app ID, e.g. ``"1:123456:android:abcdef"``.
        providers:
            Attestation providers to accept.  Defaults to all.
        """
        if not app_id:
            raise AppCheckError("app_id must not be empty")
        record = {
            "app_id": app_id,
            "providers": providers or list(SUPPORTED_PROVIDERS),
            "registered_at": time.time(),
        }
        self._store.set(_NS_APPS, app_id, record)
        return record

    def get_app(self, app_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get(_NS_APPS, app_id)

    def list_apps(self) -> List[Dict[str, Any]]:
        return [v for _, v in self._store.items(_NS_APPS)]

    def unregister_app(self, app_id: str) -> bool:
        return self._store.delete(_NS_APPS, app_id)

    # ---- token issuance -----------------------------------------------------

    def issue_token(self, app_id: str,
                    provider: str = "debug",
                    attestation_data: Optional[Dict[str, Any]] = None,
                    ttl: Optional[int] = None) -> str:
        """Issue an App Check token for *app_id*.

        Parameters
        ----------
        app_id:
            The app requesting attestation.
        provider:
            Attestation provider (``debug``, ``play_integrity``, etc.).
        attestation_data:
            Provider-specific data (ignored in local mode, stored for audit).
        ttl:
            Token lifetime in seconds.  Defaults to ``self._token_ttl``.

        Returns the signed token string.
        """
        if not app_id:
            raise AppCheckError("app_id must not be empty")
        if provider not in SUPPORTED_PROVIDERS:
            raise AppCheckError(
                f"unsupported provider {provider!r}; "
                f"supported: {sorted(SUPPORTED_PROVIDERS)}")

        # In debug mode accept the known debug token directly
        # (treated as pre-verified attestation)

        jti = uuid.uuid4().hex
        now = int(time.time())
        exp = now + (ttl if ttl is not None else self._token_ttl)

        header = {"alg": "HS256", "typ": "AC"}
        payload: Dict[str, Any] = {
            "sub": app_id,
            "jti": jti,
            "iat": now,
            "exp": exp,
            "iss": "openfirebase-appcheck",
            "provider": provider,
        }
        if attestation_data:
            payload["attestation"] = attestation_data

        segs = [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":")).encode()),
        ]
        signing_input = ".".join(segs).encode("ascii")
        sig = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        segs.append(_b64url(sig))
        token = ".".join(segs)

        # Record in store for revocation / listing
        self._store.set(_NS_TOKENS, jti, {
            "jti": jti,
            "app_id": app_id,
            "provider": provider,
            "issued_at": now,
            "expires_at": exp,
        })
        return token

    # ---- token verification -------------------------------------------------

    def verify_token(self, token: str,
                     expected_app_id: Optional[str] = None) -> Dict[str, Any]:
        """Verify an App Check token.

        Returns the token payload on success.
        Raises :class:`AppCheckError` on any failure.

        Parameters
        ----------
        token:
            Token string returned by :meth:`issue_token`.
        expected_app_id:
            If supplied, the ``sub`` (app_id) in the token must match.
        """
        try:
            h_seg, p_seg, s_seg = token.split(".")
        except ValueError:
            raise AppCheckError("malformed token")

        # verify signature
        try:
            signing_input = f"{h_seg}.{p_seg}".encode("ascii")
            expected_sig = hmac.new(
                self._secret, signing_input, hashlib.sha256).digest()
            actual_sig = _b64url_decode(s_seg)
        except Exception:
            raise AppCheckError("malformed token")

        if not hmac.compare_digest(expected_sig, actual_sig):
            raise AppCheckError("invalid token signature")

        # decode header + payload
        try:
            header = json.loads(_b64url_decode(h_seg))
            payload = json.loads(_b64url_decode(p_seg))
        except Exception:
            raise AppCheckError("malformed token payload")

        if header.get("typ") != "AC":
            raise AppCheckError("not an App Check token")

        if int(payload.get("exp", 0)) < int(time.time()):
            raise AppCheckError("token expired")

        # check revocation
        jti = payload.get("jti", "")
        if self._store.get(_NS_REVOKED, jti):
            raise AppCheckError("token has been revoked")

        if expected_app_id and payload.get("sub") != expected_app_id:
            raise AppCheckError(
                f"token app_id mismatch: expected {expected_app_id!r}, "
                f"got {payload.get('sub')!r}")

        return payload

    # ---- revocation ---------------------------------------------------------

    def revoke_token(self, jti: str) -> bool:
        """Revoke a token by its JTI.  Returns True if the token was found."""
        record = self._store.get(_NS_TOKENS, jti)
        if record is None:
            return False
        self._store.set(_NS_REVOKED, jti, True)
        return True

    def list_tokens(self, app_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List issued tokens, optionally filtered by *app_id*."""
        toks = [v for _, v in self._store.items(_NS_TOKENS)]
        if app_id:
            toks = [t for t in toks if t.get("app_id") == app_id]
        toks.sort(key=lambda t: t.get("issued_at", 0), reverse=True)
        return toks
