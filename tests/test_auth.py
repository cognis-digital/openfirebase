import time

import pytest

from openfirebase.auth import AuthService, AuthError


@pytest.fixture
def auth():
    return AuthService(secret="test-secret")


def test_sign_up_returns_public_user(auth):
    user = auth.sign_up("Ada@Example.com", "hunter2", display_name="Ada")
    assert user["email"] == "ada@example.com"  # normalised
    assert user["display_name"] == "Ada"
    assert "uid" in user
    assert "password_hash" not in user


def test_sign_up_duplicate_email(auth):
    auth.sign_up("a@b.com", "secret1")
    with pytest.raises(AuthError):
        auth.sign_up("a@b.com", "secret2")


def test_sign_up_rejects_bad_email(auth):
    with pytest.raises(AuthError):
        auth.sign_up("notanemail", "secret1")


def test_sign_up_rejects_short_password(auth):
    with pytest.raises(AuthError):
        auth.sign_up("a@b.com", "x")


def test_sign_in_success_issues_token(auth):
    auth.sign_up("a@b.com", "secret1")
    res = auth.sign_in("a@b.com", "secret1")
    assert res["user"]["email"] == "a@b.com"
    assert res["id_token"].count(".") == 2


def test_sign_in_wrong_password(auth):
    auth.sign_up("a@b.com", "secret1")
    with pytest.raises(AuthError):
        auth.sign_in("a@b.com", "wrong")


def test_sign_in_unknown_user(auth):
    with pytest.raises(AuthError):
        auth.sign_in("ghost@b.com", "secret1")


def test_verify_token_roundtrip(auth):
    auth.sign_up("a@b.com", "secret1")
    token = auth.sign_in("a@b.com", "secret1")["id_token"]
    claims = auth.verify_token(token)
    assert claims["iss"] == "openfirebase"
    assert "sub" in claims


def test_verify_tampered_token(auth):
    auth.sign_up("a@b.com", "secret1")
    token = auth.sign_in("a@b.com", "secret1")["id_token"]
    h, p, s = token.split(".")
    tampered = ".".join([h, p, s[:-2] + ("AA" if not s.endswith("AA") else "BB")])
    with pytest.raises(AuthError):
        auth.verify_token(tampered)


def test_verify_malformed_token(auth):
    with pytest.raises(AuthError):
        auth.verify_token("not-a-token")


def test_verify_expired_token():
    auth = AuthService(secret="s", token_ttl=-1)
    auth.sign_up("a@b.com", "secret1")
    token = auth.issue_token(auth.get_user_uid("a@b.com"))
    with pytest.raises(AuthError):
        auth.verify_token(token)


def test_wrong_secret_fails_verification():
    a1 = AuthService(secret="secret-one")
    a1.sign_up("a@b.com", "secret1")
    token = a1.sign_in("a@b.com", "secret1")["id_token"]
    a2 = AuthService(secret="secret-two")
    with pytest.raises(AuthError):
        a2.verify_token(token)


def test_get_and_delete_user(auth):
    user = auth.sign_up("a@b.com", "secret1")
    assert auth.get_user(user["uid"])["email"] == "a@b.com"
    assert auth.delete_user(user["uid"]) is True
    assert auth.get_user(user["uid"]) is None
    assert auth.delete_user(user["uid"]) is False
