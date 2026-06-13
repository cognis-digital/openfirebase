"""End-to-end tests for the identity+security pass endpoints.

Starts the real HTTP server in a background thread and round-trips every new
service over the wire:

Security Rules:
* POST /v1/rules/load — load a rules DSL string
* POST /v1/rules/check — evaluate a rule (allowed / denied)

Remote Config:
* POST /v1/remoteconfig/parameters — create parameter
* GET  /v1/remoteconfig/parameters — list
* GET  /v1/remoteconfig/parameters/<key> — get
* DELETE /v1/remoteconfig/parameters/<key> — delete
* POST /v1/remoteconfig/conditions — create condition
* GET  /v1/remoteconfig/conditions — list
* GET  /v1/remoteconfig/conditions/<name> — get
* POST /v1/remoteconfig/fetch — evaluate config for a client context
* GET  /v1/remoteconfig/template — full template

Cloud Messaging:
* POST /v1/messaging/tokens — register token
* GET  /v1/messaging/tokens — list tokens
* GET  /v1/messaging/tokens/<tok> — get token
* DELETE /v1/messaging/tokens/<tok> — unregister
* GET  /v1/messaging/topics — list topics
* POST /v1/messaging/topics/<topic>/subscribe — subscribe token
* POST /v1/messaging/topics/<topic>/unsubscribe — unsubscribe token
* POST /v1/messaging/send — send to token / topic / multicast
* GET  /v1/messaging/messages — list inbox
* GET  /v1/messaging/messages/<id> — get message

App Check:
* POST /v1/appcheck/apps — register app
* GET  /v1/appcheck/apps — list apps
* GET  /v1/appcheck/apps/<id> — get app
* DELETE /v1/appcheck/apps/<id> — unregister app
* POST /v1/appcheck/tokens — issue token
* GET  /v1/appcheck/tokens — list tokens
* POST /v1/appcheck/tokens/verify — verify token
* POST /v1/appcheck/tokens/<jti>/revoke — revoke token
"""

import json
import urllib.request
import urllib.error

import pytest

from openfirebase.server import App, run_in_thread


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


@pytest.fixture
def server():
    app = App(data_dir=None, secret="e2e-security-secret")
    httpd, thread, port = run_in_thread(port=0, app=app)
    yield {"port": port}
    httpd.shutdown()


# ===========================================================================
# Security Rules
# ===========================================================================

_BASIC_RULES_SRC = """
service cloud.firestore {
    match /users/{uid} {
        allow read: if true;
        allow write: if request.auth != null && request.auth.uid == uid;
    }
    match /private/{doc} {
        allow read, write: if false;
    }
}
"""


class TestRulesE2E:
    def test_load_rules_ok(self, server):
        status, body = _req(server["port"], "POST", "/v1/rules/load",
                            {"rules": _BASIC_RULES_SRC})
        assert status == 200
        assert body["status"] == "ok"

    def test_check_allowed(self, server):
        _req(server["port"], "POST", "/v1/rules/load",
             {"rules": _BASIC_RULES_SRC})
        status, body = _req(server["port"], "POST", "/v1/rules/check", {
            "service": "cloud.firestore",
            "path": "/users/u1",
            "operation": "get",
            "auth": None,
        })
        assert status == 200
        assert body["allowed"] is True

    def test_check_write_allowed_matching_uid(self, server):
        _req(server["port"], "POST", "/v1/rules/load",
             {"rules": _BASIC_RULES_SRC})
        status, body = _req(server["port"], "POST", "/v1/rules/check", {
            "service": "cloud.firestore",
            "path": "/users/u1",
            "operation": "create",
            "auth": {"sub": "u1"},
        })
        assert status == 200
        assert body["allowed"] is True

    def test_check_write_denied_wrong_uid(self, server):
        _req(server["port"], "POST", "/v1/rules/load",
             {"rules": _BASIC_RULES_SRC})
        status, body = _req(server["port"], "POST", "/v1/rules/check", {
            "service": "cloud.firestore",
            "path": "/users/u1",
            "operation": "create",
            "auth": {"sub": "u2"},
        })
        assert status == 200
        assert body["allowed"] is False

    def test_check_private_denied(self, server):
        _req(server["port"], "POST", "/v1/rules/load",
             {"rules": _BASIC_RULES_SRC})
        status, body = _req(server["port"], "POST", "/v1/rules/check", {
            "service": "cloud.firestore",
            "path": "/private/secret",
            "operation": "get",
            "auth": {"sub": "u1"},
        })
        assert status == 200
        assert body["allowed"] is False

    def test_load_rules_missing_field(self, server):
        status, body = _req(server["port"], "POST", "/v1/rules/load", {})
        assert status == 400

    def test_load_rules_wrong_method(self, server):
        status, _ = _req(server["port"], "GET", "/v1/rules/load")
        assert status == 405

    def test_check_wrong_method(self, server):
        status, _ = _req(server["port"], "GET", "/v1/rules/check")
        assert status == 405


# ===========================================================================
# Remote Config
# ===========================================================================

class TestRemoteConfigE2E:
    def test_create_parameter(self, server):
        status, body = _req(server["port"], "POST",
                            "/v1/remoteconfig/parameters",
                            {"key": "theme", "default_value": "light"})
        assert status == 201
        assert body["key"] == "theme"

    def test_list_parameters(self, server):
        _req(server["port"], "POST", "/v1/remoteconfig/parameters",
             {"key": "p1", "default_value": "v1"})
        status, body = _req(server["port"], "GET",
                            "/v1/remoteconfig/parameters")
        assert status == 200
        assert any(p["key"] == "p1" for p in body["parameters"])

    def test_get_parameter(self, server):
        _req(server["port"], "POST", "/v1/remoteconfig/parameters",
             {"key": "greet", "default_value": "Hello"})
        status, body = _req(server["port"], "GET",
                            "/v1/remoteconfig/parameters/greet")
        assert status == 200
        assert body["default_value"] == "Hello"

    def test_get_missing_parameter(self, server):
        status, _ = _req(server["port"], "GET",
                         "/v1/remoteconfig/parameters/no_such")
        assert status == 404

    def test_delete_parameter(self, server):
        _req(server["port"], "POST", "/v1/remoteconfig/parameters",
             {"key": "del_me", "default_value": "x"})
        status, body = _req(server["port"], "DELETE",
                            "/v1/remoteconfig/parameters/del_me")
        assert status == 200
        assert body["deleted"] is True

    def test_create_condition(self, server):
        status, body = _req(server["port"], "POST",
                            "/v1/remoteconfig/conditions", {
                                "name": "ios",
                                "expression": [
                                    {"field": "platform", "op": "==", "value": "ios"}
                                ]
                            })
        assert status == 201
        assert body["name"] == "ios"

    def test_list_conditions(self, server):
        _req(server["port"], "POST", "/v1/remoteconfig/conditions",
             {"name": "cond1", "expression": []})
        status, body = _req(server["port"], "GET",
                            "/v1/remoteconfig/conditions")
        assert status == 200
        assert any(c["name"] == "cond1" for c in body["conditions"])

    def test_get_condition(self, server):
        _req(server["port"], "POST", "/v1/remoteconfig/conditions",
             {"name": "c_get", "expression": []})
        status, body = _req(server["port"], "GET",
                            "/v1/remoteconfig/conditions/c_get")
        assert status == 200
        assert body["name"] == "c_get"

    def test_delete_condition(self, server):
        _req(server["port"], "POST", "/v1/remoteconfig/conditions",
             {"name": "c_del", "expression": []})
        status, body = _req(server["port"], "DELETE",
                            "/v1/remoteconfig/conditions/c_del")
        assert status == 200
        assert body["deleted"] is True

    def test_fetch_with_condition(self, server):
        # set up condition + parameter
        _req(server["port"], "POST", "/v1/remoteconfig/conditions", {
            "name": "android",
            "expression": [{"field": "platform", "op": "==", "value": "android"}]
        })
        _req(server["port"], "POST", "/v1/remoteconfig/parameters", {
            "key": "color",
            "default_value": "white",
            "conditional_values": [{"condition": "android", "value": "green"}]
        })
        # fetch for android
        status, body = _req(server["port"], "POST", "/v1/remoteconfig/fetch",
                            {"client_context": {"platform": "android"}})
        assert status == 200
        assert body["config"]["color"] == "green"

    def test_fetch_default_value(self, server):
        _req(server["port"], "POST", "/v1/remoteconfig/parameters",
             {"key": "bg", "default_value": "blue"})
        status, body = _req(server["port"], "POST", "/v1/remoteconfig/fetch",
                            {"client_context": {}})
        assert status == 200
        assert body["config"]["bg"] == "blue"

    def test_get_template(self, server):
        _req(server["port"], "POST", "/v1/remoteconfig/parameters",
             {"key": "tmpl_p", "default_value": "tmpl_v"})
        status, body = _req(server["port"], "GET",
                            "/v1/remoteconfig/template")
        assert status == 200
        assert "version" in body
        assert "tmpl_p" in body["parameters"]


# ===========================================================================
# Cloud Messaging
# ===========================================================================

class TestMessagingE2E:
    def test_register_token(self, server):
        status, body = _req(server["port"], "POST", "/v1/messaging/tokens",
                            {"token": "device_abc",
                             "metadata": {"platform": "ios"}})
        assert status == 201
        assert body["token"] == "device_abc"

    def test_list_tokens(self, server):
        _req(server["port"], "POST", "/v1/messaging/tokens",
             {"token": "tok_list1"})
        status, body = _req(server["port"], "GET", "/v1/messaging/tokens")
        assert status == 200
        assert any(t["token"] == "tok_list1" for t in body["tokens"])

    def test_get_token(self, server):
        _req(server["port"], "POST", "/v1/messaging/tokens",
             {"token": "tok_get1"})
        status, body = _req(server["port"], "GET",
                            "/v1/messaging/tokens/tok_get1")
        assert status == 200
        assert body["token"] == "tok_get1"

    def test_get_unknown_token(self, server):
        status, _ = _req(server["port"], "GET",
                         "/v1/messaging/tokens/does_not_exist")
        assert status == 404

    def test_delete_token(self, server):
        _req(server["port"], "POST", "/v1/messaging/tokens",
             {"token": "tok_del"})
        status, body = _req(server["port"], "DELETE",
                            "/v1/messaging/tokens/tok_del")
        assert status == 200
        assert body["deleted"] is True

    def test_subscribe_and_list_topics(self, server):
        _req(server["port"], "POST", "/v1/messaging/topics/sports/subscribe",
             {"token": "fan_tok"})
        status, body = _req(server["port"], "GET", "/v1/messaging/topics")
        assert status == 200
        assert any(t["topic"] == "sports" for t in body["topics"])

    def test_subscribe_and_unsubscribe(self, server):
        _req(server["port"], "POST", "/v1/messaging/topics/news/subscribe",
             {"token": "subscriber"})
        status, body = _req(server["port"], "POST",
                            "/v1/messaging/topics/news/unsubscribe",
                            {"token": "subscriber"})
        assert status == 200
        assert body["removed"] is True

    def test_get_topic(self, server):
        _req(server["port"], "POST", "/v1/messaging/topics/music/subscribe",
             {"token": "listener"})
        status, body = _req(server["port"], "GET",
                            "/v1/messaging/topics/music")
        assert status == 200
        assert "listener" in body["tokens"]

    def test_send_to_token(self, server):
        status, body = _req(server["port"], "POST", "/v1/messaging/send", {
            "target_type": "token",
            "token": "device_x",
            "notification": {"title": "Hello", "body": "World"},
            "data": {"key": "val"},
        })
        assert status == 201
        assert body["target"] == "device_x"
        assert "message_id" in body

    def test_send_to_topic(self, server):
        _req(server["port"], "POST", "/v1/messaging/topics/alerts/subscribe",
             {"token": "sub1"})
        status, body = _req(server["port"], "POST", "/v1/messaging/send", {
            "target_type": "topic",
            "topic": "alerts",
            "notification": {"title": "Alert"},
        })
        assert status == 201
        assert body["target"] == "alerts"
        assert "sub1" in body["recipients"]

    def test_send_multicast(self, server):
        status, body = _req(server["port"], "POST", "/v1/messaging/send", {
            "target_type": "multicast",
            "tokens": ["t1", "t2"],
            "notification": {"title": "Multi"},
        })
        assert status == 201
        assert set(body["target"]) == {"t1", "t2"}

    def test_send_unknown_target_type(self, server):
        status, _ = _req(server["port"], "POST", "/v1/messaging/send", {
            "target_type": "bogus",
        })
        assert status == 400

    def test_list_messages(self, server):
        _req(server["port"], "POST", "/v1/messaging/send",
             {"target_type": "token", "token": "ttok"})
        status, body = _req(server["port"], "GET", "/v1/messaging/messages")
        assert status == 200
        assert len(body["messages"]) >= 1

    def test_get_message(self, server):
        _, sent = _req(server["port"], "POST", "/v1/messaging/send",
                       {"target_type": "token", "token": "atk"})
        msg_id = sent["message_id"]
        status, body = _req(server["port"], "GET",
                            f"/v1/messaging/messages/{msg_id}")
        assert status == 200
        assert body["message_id"] == msg_id

    def test_get_unknown_message(self, server):
        status, _ = _req(server["port"], "GET",
                         "/v1/messaging/messages/no_such_id")
        assert status == 404


# ===========================================================================
# App Check
# ===========================================================================

class TestAppCheckE2E:
    def test_register_app(self, server):
        status, body = _req(server["port"], "POST", "/v1/appcheck/apps",
                            {"app_id": "1:test:android:xyz"})
        assert status == 201
        assert body["app_id"] == "1:test:android:xyz"

    def test_list_apps(self, server):
        _req(server["port"], "POST", "/v1/appcheck/apps",
             {"app_id": "list_app"})
        status, body = _req(server["port"], "GET", "/v1/appcheck/apps")
        assert status == 200
        assert any(a["app_id"] == "list_app" for a in body["apps"])

    def test_get_app(self, server):
        _req(server["port"], "POST", "/v1/appcheck/apps",
             {"app_id": "get_app"})
        status, body = _req(server["port"], "GET",
                            "/v1/appcheck/apps/get_app")
        assert status == 200
        assert body["app_id"] == "get_app"

    def test_get_unknown_app(self, server):
        status, _ = _req(server["port"], "GET",
                         "/v1/appcheck/apps/no_such_app")
        assert status == 404

    def test_delete_app(self, server):
        _req(server["port"], "POST", "/v1/appcheck/apps",
             {"app_id": "del_app"})
        status, body = _req(server["port"], "DELETE",
                            "/v1/appcheck/apps/del_app")
        assert status == 200
        assert body["deleted"] is True

    def test_issue_token(self, server):
        status, body = _req(server["port"], "POST", "/v1/appcheck/tokens",
                            {"app_id": "myapp", "provider": "debug"})
        assert status == 201
        assert "token" in body
        assert body["token"].count(".") == 2

    def test_issue_token_unknown_provider(self, server):
        status, body = _req(server["port"], "POST", "/v1/appcheck/tokens",
                            {"app_id": "myapp", "provider": "fake"})
        assert status == 400

    def test_list_tokens(self, server):
        _req(server["port"], "POST", "/v1/appcheck/tokens",
             {"app_id": "app_list"})
        status, body = _req(server["port"], "GET", "/v1/appcheck/tokens")
        assert status == 200
        assert any(t["app_id"] == "app_list" for t in body["tokens"])

    def test_verify_token(self, server):
        _, issued = _req(server["port"], "POST", "/v1/appcheck/tokens",
                         {"app_id": "verify_app"})
        token = issued["token"]
        status, body = _req(server["port"], "POST",
                            "/v1/appcheck/tokens/verify",
                            {"token": token})
        assert status == 200
        assert body["valid"] is True
        assert body["claims"]["sub"] == "verify_app"

    def test_verify_token_with_app_id_check(self, server):
        _, issued = _req(server["port"], "POST", "/v1/appcheck/tokens",
                         {"app_id": "app_check_id"})
        token = issued["token"]
        status, body = _req(server["port"], "POST",
                            "/v1/appcheck/tokens/verify",
                            {"token": token, "app_id": "app_check_id"})
        assert status == 200
        assert body["valid"] is True

    def test_verify_token_app_id_mismatch(self, server):
        _, issued = _req(server["port"], "POST", "/v1/appcheck/tokens",
                         {"app_id": "real_app"})
        token = issued["token"]
        status, body = _req(server["port"], "POST",
                            "/v1/appcheck/tokens/verify",
                            {"token": token, "app_id": "wrong_app"})
        assert status == 401

    def test_verify_invalid_token(self, server):
        status, body = _req(server["port"], "POST",
                            "/v1/appcheck/tokens/verify",
                            {"token": "not.a.token"})
        assert status == 401

    def test_revoke_token(self, server):
        _, issued = _req(server["port"], "POST", "/v1/appcheck/tokens",
                         {"app_id": "revoke_app"})
        token = issued["token"]
        # get jti
        _, verified = _req(server["port"], "POST",
                           "/v1/appcheck/tokens/verify", {"token": token})
        jti = verified["claims"]["jti"]
        # revoke
        status, body = _req(server["port"], "POST",
                            f"/v1/appcheck/tokens/{jti}/revoke", {})
        assert status == 200
        assert body["revoked"] is True
        # verify should now fail
        status2, _ = _req(server["port"], "POST",
                          "/v1/appcheck/tokens/verify", {"token": token})
        assert status2 == 401
