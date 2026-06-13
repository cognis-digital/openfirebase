"""Unit tests for the deep Auth features added in the messaging+compute pass.

Covers:
* Custom-token mint + verify
* ID-token with custom claims
* update_user (email, password, display_name, disabled, email_verified, custom_claims)
* list_users (pagination)
* password-reset OTP flow (generate → confirm)
* email-verification OTP flow (generate → confirm)
* provider sign-in stubs (new + existing user)
* set_custom_claims
* disabled user sign-in rejection
"""

import time

import pytest

from openfirebase.auth import AuthService, AuthError
from openfirebase.storage import MemoryStore


@pytest.fixture
def auth():
    return AuthService(store=MemoryStore(), secret="deep-test-secret")


# ---- Custom token -------------------------------------------------------

def test_mint_and_verify_custom_token(auth):
    user = auth.sign_up("a@b.com", "password1")
    token = auth.mint_custom_token(user["uid"], custom_claims={"role": "admin"})
    assert token.count(".") == 2
    payload = auth.verify_custom_token(token)
    assert payload["sub"] == user["uid"]
    assert payload["custom_claims"] == {"role": "admin"}


def test_custom_token_wrong_secret_fails(auth):
    user = auth.sign_up("a@b.com", "password1")
    token = auth.mint_custom_token(user["uid"])
    other = AuthService(secret="other-secret")
    with pytest.raises(AuthError):
        other.verify_custom_token(token)


def test_custom_token_expired_fails(auth):
    user = auth.sign_up("a@b.com", "password1")
    token = auth.mint_custom_token(user["uid"], ttl=-1)
    with pytest.raises(AuthError, match="expired"):
        auth.verify_custom_token(token)


def test_verify_custom_token_rejects_id_token(auth):
    """An id-token (typ=OFB) must be rejected by verify_custom_token."""
    user = auth.sign_up("a@b.com", "password1")
    id_token = auth.issue_token(user["uid"])
    with pytest.raises(AuthError):
        auth.verify_custom_token(id_token)


def test_custom_token_no_claims(auth):
    user = auth.sign_up("a@b.com", "password1")
    token = auth.mint_custom_token(user["uid"])
    payload = auth.verify_custom_token(token)
    assert payload["custom_claims"] == {}


# ---- ID-token with custom claims ----------------------------------------

def test_id_token_includes_custom_claims(auth):
    user = auth.sign_up("a@b.com", "password1")
    auth.set_custom_claims(user["uid"], {"plan": "pro"})
    token = auth.issue_token(user["uid"])
    payload = auth.verify_token(token)
    assert payload.get("claims", {}).get("plan") == "pro"


def test_id_token_no_claims_no_claims_key(auth):
    """When there are no custom claims, the 'claims' key should not appear."""
    user = auth.sign_up("a@b.com", "password1")
    token = auth.issue_token(user["uid"])
    payload = auth.verify_token(token)
    # either absent or empty — both are acceptable
    assert not payload.get("claims")


# ---- update_user --------------------------------------------------------

def test_update_display_name(auth):
    user = auth.sign_up("a@b.com", "password1", "Old Name")
    updated = auth.update_user(user["uid"], display_name="New Name")
    assert updated["display_name"] == "New Name"
    assert auth.get_user(user["uid"])["display_name"] == "New Name"


def test_update_email(auth):
    user = auth.sign_up("a@b.com", "password1")
    updated = auth.update_user(user["uid"], email="new@b.com")
    assert updated["email"] == "new@b.com"
    # old email index should be gone
    assert auth.get_user_uid("a@b.com") is None
    assert auth.get_user_uid("new@b.com") == user["uid"]


def test_update_email_duplicate_raises(auth):
    u1 = auth.sign_up("a@b.com", "password1")
    auth.sign_up("b@b.com", "password2")
    with pytest.raises(AuthError, match="already registered"):
        auth.update_user(u1["uid"], email="b@b.com")


def test_update_password_allows_new_signin(auth):
    user = auth.sign_up("a@b.com", "password1")
    auth.update_user(user["uid"], password="newpass1")
    result = auth.sign_in("a@b.com", "newpass1")
    assert "id_token" in result
    with pytest.raises(AuthError):
        auth.sign_in("a@b.com", "password1")


def test_update_password_too_short_raises(auth):
    user = auth.sign_up("a@b.com", "password1")
    with pytest.raises(AuthError, match="6"):
        auth.update_user(user["uid"], password="abc")


def test_update_disabled_blocks_signin(auth):
    user = auth.sign_up("a@b.com", "password1")
    auth.update_user(user["uid"], disabled=True)
    assert auth.get_user(user["uid"])["disabled"] is True
    with pytest.raises(AuthError, match="disabled"):
        auth.sign_in("a@b.com", "password1")


def test_re_enable_user(auth):
    user = auth.sign_up("a@b.com", "password1")
    auth.update_user(user["uid"], disabled=True)
    auth.update_user(user["uid"], disabled=False)
    result = auth.sign_in("a@b.com", "password1")
    assert "id_token" in result


def test_update_email_verified(auth):
    user = auth.sign_up("a@b.com", "password1")
    assert auth.get_user(user["uid"])["email_verified"] is False
    auth.update_user(user["uid"], email_verified=True)
    assert auth.get_user(user["uid"])["email_verified"] is True


def test_update_custom_claims(auth):
    user = auth.sign_up("a@b.com", "password1")
    auth.update_user(user["uid"], custom_claims={"tier": "gold"})
    assert auth.get_user(user["uid"])["custom_claims"] == {"tier": "gold"}


def test_update_custom_claims_must_be_dict(auth):
    user = auth.sign_up("a@b.com", "password1")
    with pytest.raises(AuthError):
        auth.update_user(user["uid"], custom_claims="not-a-dict")


def test_update_nonexistent_user_raises(auth):
    with pytest.raises(AuthError):
        auth.update_user("no-such-uid", display_name="X")


# ---- list_users ---------------------------------------------------------

def test_list_users_empty(auth):
    result = auth.list_users()
    assert result["users"] == []
    assert result["next_page_token"] is None


def test_list_users_returns_all(auth):
    for i in range(5):
        auth.sign_up(f"u{i}@b.com", "password1")
    result = auth.list_users()
    assert len(result["users"]) == 5
    assert result["next_page_token"] is None


def test_list_users_pagination(auth):
    for i in range(6):
        auth.sign_up(f"v{i}@b.com", "password1")
    page1 = auth.list_users(page_size=4)
    assert len(page1["users"]) == 4
    assert page1["next_page_token"] is not None
    page2 = auth.list_users(page_size=4, page_token=page1["next_page_token"])
    assert len(page2["users"]) == 2
    assert page2["next_page_token"] is None


def test_list_users_no_password_hash_exposed(auth):
    auth.sign_up("a@b.com", "password1")
    result = auth.list_users()
    for user in result["users"]:
        assert "password_hash" not in user
        assert "salt" not in user


# ---- get_user_by_email --------------------------------------------------

def test_get_user_by_email(auth):
    auth.sign_up("a@b.com", "password1", "Ada")
    user = auth.get_user_by_email("a@b.com")
    assert user is not None and user["display_name"] == "Ada"


def test_get_user_by_email_missing(auth):
    assert auth.get_user_by_email("ghost@b.com") is None


# ---- Password reset flow ------------------------------------------------

def test_password_reset_flow(auth):
    user = auth.sign_up("a@b.com", "password1")
    token = auth.generate_password_reset_token("a@b.com")
    assert isinstance(token, str) and len(token) == 64
    auth.confirm_password_reset(token, "newpassword1")
    result = auth.sign_in("a@b.com", "newpassword1")
    assert "id_token" in result


def test_password_reset_invalid_token(auth):
    with pytest.raises(AuthError, match="invalid"):
        auth.confirm_password_reset("badtoken", "newpassword1")


def test_password_reset_consumed(auth):
    auth.sign_up("a@b.com", "password1")
    token = auth.generate_password_reset_token("a@b.com")
    auth.confirm_password_reset(token, "newpassword1")
    # second use of same token must fail
    with pytest.raises(AuthError):
        auth.confirm_password_reset(token, "anotherpassword")


def test_password_reset_unknown_email(auth):
    with pytest.raises(AuthError, match="no such user"):
        auth.generate_password_reset_token("ghost@b.com")


def test_password_reset_expired(auth):
    auth.sign_up("a@b.com", "password1")
    token = auth.generate_password_reset_token("a@b.com")
    # Force expiry by manipulating the store
    store = auth._store
    rec = store.get("auth::pw_reset", token)
    rec = dict(rec)
    rec["expires"] = time.time() - 1
    store.set("auth::pw_reset", token, rec)
    with pytest.raises(AuthError, match="expired"):
        auth.confirm_password_reset(token, "newpassword1")


# ---- Email verification flow --------------------------------------------

def test_email_verification_flow(auth):
    user = auth.sign_up("a@b.com", "password1")
    assert auth.get_user(user["uid"])["email_verified"] is False
    token = auth.generate_email_verification_token(user["uid"])
    assert isinstance(token, str) and len(token) == 64
    updated = auth.confirm_email_verification(token)
    assert updated["email_verified"] is True
    assert auth.get_user(user["uid"])["email_verified"] is True


def test_email_verification_invalid_token(auth):
    with pytest.raises(AuthError, match="invalid"):
        auth.confirm_email_verification("badtoken")


def test_email_verification_consumed(auth):
    user = auth.sign_up("a@b.com", "password1")
    token = auth.generate_email_verification_token(user["uid"])
    auth.confirm_email_verification(token)
    with pytest.raises(AuthError):
        auth.confirm_email_verification(token)


def test_email_verification_unknown_uid(auth):
    with pytest.raises(AuthError):
        auth.generate_email_verification_token("no-such-uid")


def test_email_verification_expired(auth):
    user = auth.sign_up("a@b.com", "password1")
    token = auth.generate_email_verification_token(user["uid"])
    rec = auth._store.get("auth::email_verify", token)
    rec = dict(rec)
    rec["expires"] = time.time() - 1
    auth._store.set("auth::email_verify", token, rec)
    with pytest.raises(AuthError, match="expired"):
        auth.confirm_email_verification(token)


# ---- Provider sign-in stubs --------------------------------------------

def test_provider_signin_new_user(auth):
    result = auth.sign_in_with_provider("google.com", "google-uid-1",
                                        email="ada@gmail.com",
                                        display_name="Ada")
    assert "user" in result and "id_token" in result
    user = result["user"]
    assert user["email"] == "ada@gmail.com"
    assert user["display_name"] == "Ada"
    pd = user["provider_data"]
    assert any(p["provider_id"] == "google.com" and p["uid"] == "google-uid-1"
               for p in pd)


def test_provider_signin_returns_existing(auth):
    r1 = auth.sign_in_with_provider("google.com", "google-uid-2",
                                    email="bob@gmail.com")
    r2 = auth.sign_in_with_provider("google.com", "google-uid-2",
                                    email="bob@gmail.com")
    assert r1["user"]["uid"] == r2["user"]["uid"]


def test_provider_signin_links_to_existing_email(auth):
    """If the email already belongs to an email/password user, link the provider."""
    user = auth.sign_up("charlie@example.com", "password1")
    result = auth.sign_in_with_provider("github.com", "gh-uid-99",
                                        email="charlie@example.com")
    # must return the same uid
    assert result["user"]["uid"] == user["uid"]
    # provider_data must include the new provider
    pd = result["user"]["provider_data"]
    assert any(p["provider_id"] == "github.com" for p in pd)


def test_provider_signin_unsupported_raises(auth):
    with pytest.raises(AuthError, match="unsupported provider"):
        auth.sign_in_with_provider("unknown.com", "uid-x")


def test_provider_signin_disabled_raises(auth):
    result = auth.sign_in_with_provider("google.com", "gid-3",
                                        email="dave@g.com")
    uid = result["user"]["uid"]
    auth.update_user(uid, disabled=True)
    with pytest.raises(AuthError, match="disabled"):
        auth.sign_in_with_provider("google.com", "gid-3")


def test_provider_signin_anonymous(auth):
    r = auth.sign_in_with_provider("anonymous", "anon-uid-1")
    assert "id_token" in r
    assert r["user"]["email"] is None


# ---- set_custom_claims shortcut ----------------------------------------

def test_set_custom_claims(auth):
    user = auth.sign_up("a@b.com", "password1")
    updated = auth.set_custom_claims(user["uid"], {"admin": True})
    assert updated["custom_claims"]["admin"] is True
    assert auth.get_user(user["uid"])["custom_claims"]["admin"] is True


def test_set_custom_claims_overwrite(auth):
    user = auth.sign_up("a@b.com", "password1")
    auth.set_custom_claims(user["uid"], {"a": 1, "b": 2})
    auth.set_custom_claims(user["uid"], {"c": 3})
    claims = auth.get_user(user["uid"])["custom_claims"]
    # Overwrite semantics: only {"c": 3} should remain
    assert claims == {"c": 3}


# ---- provider_data persisted after delete_user --------------------------

def test_delete_cleans_up_provider_index(auth):
    result = auth.sign_in_with_provider("google.com", "gid-delete",
                                        email="del@g.com")
    uid = result["user"]["uid"]
    auth.delete_user(uid)
    # signing in again should create a NEW user (index cleaned)
    r2 = auth.sign_in_with_provider("google.com", "gid-delete",
                                    email="del@g.com")
    assert r2["user"]["uid"] != uid
