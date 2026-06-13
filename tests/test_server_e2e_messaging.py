"""End-to-end tests for the messaging+compute pass endpoints.

Starts the real HTTP server in a background thread and exercises every new
API path over the wire:

Auth:
* custom-token mint + verify-custom-token
* /v1/auth/users  (list, GET by uid, PATCH, DELETE)
* /v1/auth/password-reset  (generate + confirm)
* /v1/auth/email-verification  (generate + confirm)
* /v1/auth/provider-signin  stub
* /v1/auth/set-custom-claims
* Auth triggers fired on sign-up / delete

Functions:
* /v1/functions  listing (extended)
* /v1/functions/_callable/<name>  (success + FunctionError)
* /v1/functions/_pubsub/<topic>  publish
* /v1/functions/_schedule/<name>  run

Hosting management:
* /v1/hosting/channels  (list, create, delete)

Storage triggers:
* Storage upload fires onStorageObjectFinalize
* Storage delete fires onStorageObjectDelete

Hosting serving:
* Redirects (301 / 302)
* Rewrites → function proxy
* Custom headers in response
"""

import base64
import json
import urllib.request
import urllib.error

import pytest

from openfirebase.server import App, run_in_thread
from openfirebase.functions import (
    FunctionError,
    ON_AUTH_USER_CREATE, ON_AUTH_USER_DELETE,
    ON_STORAGE_FINALIZE, ON_STORAGE_DELETE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(port, method, path, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def _req_full(port, method, path, body=None):
    """Like _req but also returns response headers dict."""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            headers = dict(resp.headers)
            return resp.status, (json.loads(raw) if raw else None), headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None), {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server(tmp_path_factory):
    public = tmp_path_factory.mktemp("public")
    (public / "index.html").write_text("<h1>hosted</h1>", encoding="utf-8")
    (public / "about.html").write_text("<h1>about</h1>", encoding="utf-8")

    auth_creates = []
    auth_deletes = []
    storage_finalizes = []
    storage_deletes = []
    pubsub_msgs = []
    schedule_runs = []
    callable_calls = []

    app = App(data_dir=None, secret="e2e-msg-secret",
              public_dir=str(public))

    # Configure redirects + headers on the hosting instance
    app.hosting.redirects = [
        {"source": "/old-path", "destination": "/about.html", "type": 301},
        {"source": "/temp", "destination": "/index.html", "type": 302},
    ]
    app.hosting.headers_rules = [
        {"source": "**/*.html", "headers": [
            {"key": "X-Custom", "value": "test-header"},
        ]},
    ]
    app.hosting.rewrites = [
        {"source": "/via-function", "function": "echo_fn"},
    ]

    # Register functions
    @app.functions.on_request("echo_fn")
    def echo_fn(req):
        return {"echoed": req.get("path")}

    @app.functions.on_call("add")
    def add_fn(data, ctx):
        return data["a"] + data["b"]

    @app.functions.on_call("will_fail")
    def will_fail(data, ctx):
        raise FunctionError("nope", code="invalid-argument")

    @app.functions.on_pubsub("test-topic")
    def on_msg(ctx):
        pubsub_msgs.append(ctx["message"])
        return "received"

    @app.functions.schedule("hourly-job")
    def hourly(ctx):
        schedule_runs.append("ran")
        return "scheduled-done"

    @app.functions.on_auth_user(ON_AUTH_USER_CREATE)
    def on_auth_create(user):
        auth_creates.append(user["uid"])

    @app.functions.on_auth_user(ON_AUTH_USER_DELETE)
    def on_auth_delete(user):
        auth_deletes.append(user["uid"])

    @app.functions.on_storage(ON_STORAGE_FINALIZE)
    def on_storage_fin(meta):
        storage_finalizes.append(meta["name"])

    @app.functions.on_storage(ON_STORAGE_DELETE)
    def on_storage_del(meta):
        storage_deletes.append(meta["name"])

    httpd, thread, port = run_in_thread(port=0, app=app)
    yield {
        "port": port,
        "app": app,
        "auth_creates": auth_creates,
        "auth_deletes": auth_deletes,
        "storage_finalizes": storage_finalizes,
        "storage_deletes": storage_deletes,
        "pubsub_msgs": pubsub_msgs,
        "schedule_runs": schedule_runs,
    }
    httpd.shutdown()


# ---------------------------------------------------------------------------
# Auth — custom token
# ---------------------------------------------------------------------------

def test_mint_custom_token(server):
    port = server["port"]
    # sign up a user first
    status, body = _req(port, "POST", "/v1/auth/signup",
                        {"email": "ct@test.com", "password": "password1"})
    assert status == 201
    uid = body["user"]["uid"]

    status, body = _req(port, "POST", "/v1/auth/custom-token",
                        {"uid": uid, "custom_claims": {"role": "superuser"}})
    assert status == 200
    assert "custom_token" in body


def test_mint_custom_token_missing_uid(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/auth/custom-token", {})
    assert status == 400


def test_verify_custom_token(server):
    port = server["port"]
    _req(port, "POST", "/v1/auth/signup",
         {"email": "ct2@test.com", "password": "password1"})
    status, tok_body = _req(port, "GET", "/v1/auth/users")
    uid = tok_body["users"][0]["uid"]
    _, tok = _req(port, "POST", "/v1/auth/custom-token",
                  {"uid": uid, "custom_claims": {"x": 1}})
    token = tok["custom_token"]
    status, body = _req(port, "POST", "/v1/auth/verify-custom-token",
                        {"token": token})
    assert status == 200
    assert body["valid"] is True
    assert body["claims"]["custom_claims"]["x"] == 1


def test_verify_custom_token_bad(server):
    port = server["port"]
    status, _ = _req(port, "POST", "/v1/auth/verify-custom-token",
                     {"token": "bad.token.value"})
    assert status == 401


# ---------------------------------------------------------------------------
# Auth — users CRUD
# ---------------------------------------------------------------------------

def test_list_users(server):
    port = server["port"]
    status, body = _req(port, "GET", "/v1/auth/users")
    assert status == 200
    assert "users" in body


def test_get_user_by_uid(server):
    port = server["port"]
    _, signup = _req(port, "POST", "/v1/auth/signup",
                     {"email": "getuser@test.com", "password": "password1"})
    uid = signup["user"]["uid"]
    status, body = _req(port, "GET", f"/v1/auth/users/{uid}")
    assert status == 200
    assert body["email"] == "getuser@test.com"


def test_get_user_missing_uid(server):
    port = server["port"]
    status, _ = _req(port, "GET", "/v1/auth/users/nonexistent-uid")
    assert status == 404


def test_patch_user(server):
    port = server["port"]
    _, signup = _req(port, "POST", "/v1/auth/signup",
                     {"email": "patchme@test.com", "password": "password1"})
    uid = signup["user"]["uid"]
    status, body = _req(port, "PATCH", f"/v1/auth/users/{uid}",
                        {"display_name": "Patched"})
    assert status == 200
    assert body["display_name"] == "Patched"


def test_disable_and_reenable_user(server):
    port = server["port"]
    _, signup = _req(port, "POST", "/v1/auth/signup",
                     {"email": "disableme@test.com", "password": "password1"})
    uid = signup["user"]["uid"]
    # disable
    _req(port, "PATCH", f"/v1/auth/users/{uid}", {"disabled": True})
    # sign-in should fail
    status, _ = _req(port, "POST", "/v1/auth/signin",
                     {"email": "disableme@test.com", "password": "password1"})
    assert status == 401
    # re-enable
    _req(port, "PATCH", f"/v1/auth/users/{uid}", {"disabled": False})
    status, _ = _req(port, "POST", "/v1/auth/signin",
                     {"email": "disableme@test.com", "password": "password1"})
    assert status == 200


def test_delete_user_via_http(server):
    port = server["port"]
    _, signup = _req(port, "POST", "/v1/auth/signup",
                     {"email": "deleteme@test.com", "password": "password1"})
    uid = signup["user"]["uid"]
    status, body = _req(port, "DELETE", f"/v1/auth/users/{uid}")
    assert status == 200
    assert body["deleted"] is True
    status, _ = _req(port, "GET", f"/v1/auth/users/{uid}")
    assert status == 404


# ---------------------------------------------------------------------------
# Auth — password reset via HTTP
# ---------------------------------------------------------------------------

def test_password_reset_generate_and_confirm(server):
    port = server["port"]
    _req(port, "POST", "/v1/auth/signup",
         {"email": "resetme@test.com", "password": "password1"})
    status, body = _req(port, "POST", "/v1/auth/password-reset",
                        {"action": "generate", "email": "resetme@test.com"})
    assert status == 200
    assert "reset_token" in body
    token = body["reset_token"]
    status, body = _req(port, "POST", "/v1/auth/password-reset",
                        {"action": "confirm", "reset_token": token,
                         "new_password": "newpassword1"})
    assert status == 200 and body["status"] == "ok"
    # new password works
    status, _ = _req(port, "POST", "/v1/auth/signin",
                     {"email": "resetme@test.com", "password": "newpassword1"})
    assert status == 200


def test_password_reset_unknown_email(server):
    port = server["port"]
    status, _ = _req(port, "POST", "/v1/auth/password-reset",
                     {"action": "generate", "email": "ghost@test.com"})
    assert status == 400


def test_password_reset_bad_token(server):
    port = server["port"]
    status, _ = _req(port, "POST", "/v1/auth/password-reset",
                     {"action": "confirm", "reset_token": "bad",
                      "new_password": "newpassword1"})
    assert status == 400


# ---------------------------------------------------------------------------
# Auth — email verification via HTTP
# ---------------------------------------------------------------------------

def test_email_verification_generate_and_confirm(server):
    port = server["port"]
    _, signup = _req(port, "POST", "/v1/auth/signup",
                     {"email": "verifyemail@test.com", "password": "password1"})
    uid = signup["user"]["uid"]
    status, body = _req(port, "POST", "/v1/auth/email-verification",
                        {"action": "generate", "uid": uid})
    assert status == 200 and "verification_token" in body
    token = body["verification_token"]
    status, body = _req(port, "POST", "/v1/auth/email-verification",
                        {"action": "confirm", "verification_token": token})
    assert status == 200
    assert body["email_verified"] is True


def test_email_verification_bad_token(server):
    port = server["port"]
    status, _ = _req(port, "POST", "/v1/auth/email-verification",
                     {"action": "confirm", "verification_token": "bad"})
    assert status == 400


# ---------------------------------------------------------------------------
# Auth — provider sign-in
# ---------------------------------------------------------------------------

def test_provider_signin_stub(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/auth/provider-signin",
                        {"provider_id": "google.com",
                         "provider_uid": "goog-123",
                         "email": "google-user@gmail.com"})
    assert status == 200
    assert "id_token" in body
    assert body["user"]["email"] == "google-user@gmail.com"


def test_provider_signin_unsupported(server):
    port = server["port"]
    status, _ = _req(port, "POST", "/v1/auth/provider-signin",
                     {"provider_id": "unknown.io", "provider_uid": "x"})
    assert status == 400


# ---------------------------------------------------------------------------
# Auth — set custom claims
# ---------------------------------------------------------------------------

def test_set_custom_claims_via_http(server):
    port = server["port"]
    _, signup = _req(port, "POST", "/v1/auth/signup",
                     {"email": "claimsuser@test.com", "password": "password1"})
    uid = signup["user"]["uid"]
    status, body = _req(port, "POST", "/v1/auth/set-custom-claims",
                        {"uid": uid, "custom_claims": {"admin": True}})
    assert status == 200
    assert body["custom_claims"]["admin"] is True


# ---------------------------------------------------------------------------
# Auth triggers fired on sign-up / delete
# ---------------------------------------------------------------------------

def test_auth_create_trigger_fires(server):
    port = server["port"]
    before = len(server["auth_creates"])
    _req(port, "POST", "/v1/auth/signup",
         {"email": "trigger-test@test.com", "password": "password1"})
    assert len(server["auth_creates"]) == before + 1


def test_auth_delete_trigger_fires(server):
    port = server["port"]
    _, signup = _req(port, "POST", "/v1/auth/signup",
                     {"email": "delete-trigger@test.com", "password": "password1"})
    uid = signup["user"]["uid"]
    before = len(server["auth_deletes"])
    _req(port, "DELETE", f"/v1/auth/users/{uid}")
    assert len(server["auth_deletes"]) == before + 1


# ---------------------------------------------------------------------------
# Functions — extended listing
# ---------------------------------------------------------------------------

def test_functions_listing_extended(server):
    port = server["port"]
    status, body = _req(port, "GET", "/v1/functions")
    assert status == 200
    assert "callable" in body
    assert "pubsub" in body
    assert "scheduled" in body
    assert "auth" in body
    assert "storage" in body
    assert "add" in body["callable"]
    assert "test-topic" in body["pubsub"]


# ---------------------------------------------------------------------------
# Functions — callable via HTTP
# ---------------------------------------------------------------------------

def test_callable_success(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/functions/_callable/add",
                        {"data": {"a": 10, "b": 32}})
    assert status == 200
    assert body["result"] == 42


def test_callable_function_error(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/functions/_callable/will_fail",
                        {"data": {}})
    assert status == 200
    assert "error" in body
    assert body["error"]["code"] == "invalid-argument"


def test_callable_missing(server):
    port = server["port"]
    status, _ = _req(port, "POST", "/v1/functions/_callable/nosuchfn",
                     {"data": {}})
    assert status == 404


def test_callable_wrong_method(server):
    port = server["port"]
    status, _ = _req(port, "GET", "/v1/functions/_callable/add")
    assert status == 405


# ---------------------------------------------------------------------------
# Functions — Pub/Sub via HTTP
# ---------------------------------------------------------------------------

def test_pubsub_publish(server):
    port = server["port"]
    before = len(server["pubsub_msgs"])
    status, body = _req(port, "POST", "/v1/functions/_pubsub/test-topic",
                        {"message": {"event": "click"}})
    assert status == 200
    assert body["count"] == 1
    assert len(server["pubsub_msgs"]) == before + 1
    assert server["pubsub_msgs"][-1] == {"event": "click"}


def test_pubsub_no_subscribers(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/functions/_pubsub/empty-topic",
                        {"message": "ping"})
    assert status == 200
    assert body["count"] == 0


def test_pubsub_wrong_method(server):
    port = server["port"]
    status, _ = _req(port, "GET", "/v1/functions/_pubsub/test-topic")
    assert status == 405


# ---------------------------------------------------------------------------
# Functions — Scheduled via HTTP
# ---------------------------------------------------------------------------

def test_schedule_run(server):
    port = server["port"]
    before = len(server["schedule_runs"])
    status, body = _req(port, "POST", "/v1/functions/_schedule/hourly-job")
    assert status == 200
    assert body["result"] == "scheduled-done"
    assert len(server["schedule_runs"]) == before + 1


def test_schedule_missing(server):
    port = server["port"]
    status, _ = _req(port, "POST", "/v1/functions/_schedule/no-such-job")
    assert status == 404


def test_schedule_wrong_method(server):
    port = server["port"]
    status, _ = _req(port, "GET", "/v1/functions/_schedule/hourly-job")
    assert status == 405


# ---------------------------------------------------------------------------
# Storage triggers
# ---------------------------------------------------------------------------

def test_storage_finalize_trigger(server):
    port = server["port"]
    before = len(server["storage_finalizes"])
    data = base64.b64encode(b"trigger-test").decode()
    _req(port, "POST", "/v1/storage/tbucket/o/trigger.txt",
         {"base64_data": data, "content_type": "text/plain"})
    assert len(server["storage_finalizes"]) == before + 1
    assert "trigger.txt" in server["storage_finalizes"]


def test_storage_delete_trigger(server):
    port = server["port"]
    data = base64.b64encode(b"del-trigger").decode()
    _req(port, "POST", "/v1/storage/tbucket/o/del-trigger.txt",
         {"base64_data": data, "content_type": "text/plain"})
    before = len(server["storage_deletes"])
    _req(port, "DELETE", "/v1/storage/tbucket/o/del-trigger.txt")
    assert len(server["storage_deletes"]) == before + 1
    assert "del-trigger.txt" in server["storage_deletes"]


# ---------------------------------------------------------------------------
# Hosting — redirects over HTTP
# ---------------------------------------------------------------------------

def test_hosting_redirect_301(server):
    port = server["port"]
    url = f"http://127.0.0.1:{port}/old-path"
    req = urllib.request.Request(url, method="GET")
    # Don't follow redirects
    opener = urllib.request.build_opener(
        urllib.request.HTTPErrorProcessor()
    )
    try:
        opener.open(req)
    except urllib.error.HTTPError as e:
        assert e.code == 301
        assert "/about.html" in e.headers.get("Location", "")


def test_hosting_redirect_302(server):
    port = server["port"]
    url = f"http://127.0.0.1:{port}/temp"
    req = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(urllib.request.HTTPErrorProcessor())
    try:
        opener.open(req)
    except urllib.error.HTTPError as e:
        assert e.code == 302


# ---------------------------------------------------------------------------
# Hosting — rewrite → function proxy
# ---------------------------------------------------------------------------

def test_hosting_rewrite_to_function(server):
    port = server["port"]
    status, body = _req(port, "GET", "/via-function")
    assert status == 200
    assert "result" in body
    assert body["result"]["echoed"] == "/via-function"


# ---------------------------------------------------------------------------
# Hosting — custom headers injected
# ---------------------------------------------------------------------------

def test_hosting_custom_headers(server):
    port = server["port"]
    url = f"http://127.0.0.1:{port}/index.html"
    with urllib.request.urlopen(url) as resp:
        headers = dict(resp.headers)
        # Header keys are lowercase in http.client responses
        combined = {k.lower(): v for k, v in headers.items()}
        assert combined.get("x-custom") == "test-header"


# ---------------------------------------------------------------------------
# Hosting — channel management via HTTP
# ---------------------------------------------------------------------------

def test_list_channels_initially_empty(server):
    port = server["port"]
    status, body = _req(port, "GET", "/v1/hosting/channels")
    assert status == 200
    assert "channels" in body


def test_create_and_delete_channel(server, tmp_path_factory):
    port = server["port"]
    overlay = tmp_path_factory.mktemp("overlay")
    (overlay / "index.html").write_text("<h1>preview</h1>", encoding="utf-8")
    status, body = _req(port, "POST", "/v1/hosting/channels/preview-x",
                        {"dir": str(overlay)})
    assert status == 201
    assert body["name"] == "preview-x"
    # list should include it
    _, list_body = _req(port, "GET", "/v1/hosting/channels")
    names = [c["name"] for c in list_body["channels"]]
    assert "preview-x" in names
    # delete
    status, body = _req(port, "DELETE", "/v1/hosting/channels/preview-x")
    assert status == 200 and body["deleted"] is True


def test_hosting_mgmt_without_public_dir(server):
    """Hosting management when hosting IS configured should work."""
    port = server["port"]
    status, _ = _req(port, "GET", "/v1/hosting/channels")
    assert status == 200
