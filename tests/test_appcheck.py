"""Unit tests for App Check (openfirebase.appcheck)."""

import time
import pytest

from openfirebase.appcheck import AppCheck, AppCheckError, SUPPORTED_PROVIDERS
from openfirebase.storage import MemoryStore


class TestAppCheck:
    def setup_method(self):
        self.ac = AppCheck(MemoryStore(), secret="test-secret-appcheck")

    # ---- app registration ---------------------------------------------------

    def test_register_app(self):
        rec = self.ac.register_app("1:123:android:abc")
        assert rec["app_id"] == "1:123:android:abc"

    def test_register_app_empty_raises(self):
        with pytest.raises(AppCheckError):
            self.ac.register_app("")

    def test_get_app(self):
        self.ac.register_app("app1")
        rec = self.ac.get_app("app1")
        assert rec is not None
        assert rec["app_id"] == "app1"

    def test_get_unknown_app(self):
        assert self.ac.get_app("ghost") is None

    def test_list_apps(self):
        self.ac.register_app("a1")
        self.ac.register_app("a2")
        apps = self.ac.list_apps()
        ids = {a["app_id"] for a in apps}
        assert "a1" in ids
        assert "a2" in ids

    def test_unregister_app(self):
        self.ac.register_app("a_to_del")
        ok = self.ac.unregister_app("a_to_del")
        assert ok is True
        assert self.ac.get_app("a_to_del") is None

    def test_unregister_nonexistent(self):
        assert self.ac.unregister_app("nope") is False

    # ---- token issuance -----------------------------------------------------

    def test_issue_token_returns_string(self):
        token = self.ac.issue_token("app1")
        assert isinstance(token, str)
        assert token.count(".") == 2

    def test_issue_token_debug_provider(self):
        token = self.ac.issue_token("app1", provider="debug")
        payload = self.ac.verify_token(token)
        assert payload["provider"] == "debug"

    def test_issue_token_all_providers(self):
        for provider in SUPPORTED_PROVIDERS:
            token = self.ac.issue_token("app1", provider=provider)
            payload = self.ac.verify_token(token)
            assert payload["provider"] == provider

    def test_issue_token_unknown_provider_raises(self):
        with pytest.raises(AppCheckError, match="unsupported provider"):
            self.ac.issue_token("app1", provider="fake_provider")

    def test_issue_token_empty_app_id_raises(self):
        with pytest.raises(AppCheckError):
            self.ac.issue_token("")

    def test_issue_token_custom_ttl(self):
        token = self.ac.issue_token("app1", ttl=7200)
        payload = self.ac.verify_token(token)
        assert payload["exp"] - payload["iat"] == 7200

    def test_issue_token_with_attestation_data(self):
        token = self.ac.issue_token(
            "app1",
            attestation_data={"device_id": "dev123"},
        )
        payload = self.ac.verify_token(token)
        assert payload["attestation"]["device_id"] == "dev123"

    # ---- token verification -------------------------------------------------

    def test_verify_token_success(self):
        token = self.ac.issue_token("myapp")
        payload = self.ac.verify_token(token)
        assert payload["sub"] == "myapp"
        assert "jti" in payload
        assert "iss" in payload

    def test_verify_token_expected_app_id_match(self):
        token = self.ac.issue_token("app1")
        payload = self.ac.verify_token(token, expected_app_id="app1")
        assert payload["sub"] == "app1"

    def test_verify_token_expected_app_id_mismatch(self):
        token = self.ac.issue_token("app1")
        with pytest.raises(AppCheckError, match="mismatch"):
            self.ac.verify_token(token, expected_app_id="app2")

    def test_verify_token_tampered(self):
        token = self.ac.issue_token("app1")
        # flip a character in the payload segment
        parts = token.split(".")
        bad = list(parts[1])
        bad[5] = "X" if bad[5] != "X" else "Y"
        parts[1] = "".join(bad)
        tampered = ".".join(parts)
        with pytest.raises(AppCheckError):
            self.ac.verify_token(tampered)

    def test_verify_token_wrong_secret(self):
        other_ac = AppCheck(MemoryStore(), secret="different-secret")
        token = self.ac.issue_token("app1")
        with pytest.raises(AppCheckError):
            other_ac.verify_token(token)

    def test_verify_malformed_token(self):
        with pytest.raises(AppCheckError, match="malformed"):
            self.ac.verify_token("not.a.valid.token")

    def test_verify_expired_token(self):
        ac_short = AppCheck(MemoryStore(), secret="test-secret-appcheck", token_ttl=1)
        token = ac_short.issue_token("app1")
        # Mock expiry: issue with ttl=-10 (already expired)
        expired_token = ac_short.issue_token("app1", ttl=-10)
        with pytest.raises(AppCheckError, match="expired"):
            ac_short.verify_token(expired_token)

    def test_verify_wrong_token_type(self):
        """A token with typ != AC should fail."""
        from openfirebase.auth import AuthService
        auth = AuthService(MemoryStore(), secret="test-secret-appcheck")
        user = auth.sign_up("x@x.com", "password1")
        id_token = auth.issue_token(user["uid"])
        with pytest.raises(AppCheckError):
            self.ac.verify_token(id_token)

    # ---- revocation ---------------------------------------------------------

    def test_revoke_token(self):
        token = self.ac.issue_token("app1")
        payload = self.ac.verify_token(token)   # ok before revoke
        jti = payload["jti"]
        ok = self.ac.revoke_token(jti)
        assert ok is True
        with pytest.raises(AppCheckError, match="revoked"):
            self.ac.verify_token(token)

    def test_revoke_nonexistent_jti(self):
        assert self.ac.revoke_token("no-such-jti") is False

    # ---- list tokens --------------------------------------------------------

    def test_list_tokens(self):
        self.ac.issue_token("app1")
        self.ac.issue_token("app2")
        toks = self.ac.list_tokens()
        assert len(toks) >= 2

    def test_list_tokens_filtered(self):
        self.ac.issue_token("myapp")
        self.ac.issue_token("otherapp")
        toks = self.ac.list_tokens(app_id="myapp")
        assert all(t["app_id"] == "myapp" for t in toks)
        assert len(toks) >= 1

    def test_list_tokens_sorted_newest_first(self):
        self.ac.issue_token("app1")
        self.ac.issue_token("app1")
        toks = self.ac.list_tokens(app_id="app1")
        times = [t["issued_at"] for t in toks]
        assert times == sorted(times, reverse=True)
