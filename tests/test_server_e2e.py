"""End-to-end tests: start the real HTTP server in a thread and round-trip
data through every service over the wire."""

import json
import urllib.request
import urllib.error

import pytest

from openfirebase.server import App, run_in_thread


@pytest.fixture
def server(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    (public / "index.html").write_text("<h1>hosted</h1>", encoding="utf-8")
    app = App(data_dir=None, public_dir=str(public), secret="e2e-secret")

    # register a request function + a db trigger to assert wiring
    fired = []

    @app.functions.on_request("echo")
    def echo(req):
        return {"got": req["body"]}

    @app.functions.on_db("onCreate", "users/")
    def on_user_create(ctx):
        fired.append(ctx["path"])

    httpd, thread, port = run_in_thread(port=0, app=app)
    yield {"port": port, "fired": fired}
    httpd.shutdown()


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


def test_health(server):
    status, body = _req(server["port"], "GET", "/__health")
    assert status == 200 and body["status"] == "ok"


def test_firestore_crud_and_trigger(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/firestore/users",
                        {"name": "Ada", "age": 36})
    assert status == 201
    doc_id = body["id"]

    status, body = _req(port, "GET", f"/v1/firestore/users/{doc_id}")
    assert status == 200 and body["name"] == "Ada"

    status, body = _req(port, "PATCH", f"/v1/firestore/users/{doc_id}",
                        {"age": 37})
    assert status == 200

    status, body = _req(port, "GET", f"/v1/firestore/users/{doc_id}")
    assert body["age"] == 37

    status, body = _req(port, "GET", "/v1/firestore/users")
    assert len(body["documents"]) == 1

    status, body = _req(port, "DELETE", f"/v1/firestore/users/{doc_id}")
    assert body["deleted"] is True

    # onCreate trigger fired for the POST under users/
    assert any(p.startswith("users/") for p in server["fired"])


def test_firestore_missing_doc_404(server):
    status, _ = _req(server["port"], "GET", "/v1/firestore/users/ghost")
    assert status == 404


def test_rtdb_roundtrip(server):
    port = server["port"]
    status, body = _req(port, "PUT", "/v1/rtdb/rooms/r1",
                        {"value": {"name": "Lobby"}})
    assert status == 200 and body["value"]["name"] == "Lobby"

    status, body = _req(port, "PATCH", "/v1/rtdb/rooms/r1",
                        {"topic": "general"})
    assert body["value"] == {"name": "Lobby", "topic": "general"}

    status, body = _req(port, "POST", "/v1/rtdb/rooms/r1/messages",
                        {"value": {"text": "hi"}})
    assert status == 201 and "key" in body

    status, body = _req(port, "GET", "/v1/rtdb/rooms/r1")
    assert body["value"]["name"] == "Lobby"

    status, body = _req(port, "DELETE", "/v1/rtdb/rooms/r1")
    assert body["deleted"] is True


def test_auth_flow_over_http(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/auth/signup",
                        {"email": "a@b.com", "password": "secret1"})
    assert status == 201
    token = body["id_token"]

    status, body = _req(port, "POST", "/v1/auth/verify", {"id_token": token})
    assert status == 200 and body["valid"] is True

    status, body = _req(port, "POST", "/v1/auth/signin",
                        {"email": "a@b.com", "password": "secret1"})
    assert status == 200 and "id_token" in body

    status, body = _req(port, "POST", "/v1/auth/signin",
                        {"email": "a@b.com", "password": "wrong"})
    assert status == 401


def test_functions_http_invoke(server):
    status, body = _req(server["port"], "POST", "/v1/functions/echo",
                        {"name": "Bo"})
    assert status == 200 and body["result"]["got"] == {"name": "Bo"}


def test_functions_listing(server):
    status, body = _req(server["port"], "GET", "/v1/functions")
    assert "echo" in body["http"]


def test_static_hosting(server):
    port = server["port"]
    url = f"http://127.0.0.1:{port}/index.html"
    with urllib.request.urlopen(url) as resp:
        assert resp.status == 200
        assert b"hosted" in resp.read()
