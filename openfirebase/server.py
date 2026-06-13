"""Single local HTTP server exposing all openfirebase services.

Path prefixes
-------------
* ``/v1/firestore/<collection>[/<doc_id>]``  - document database (REST)
* ``/v1/rtdb/<path...>``                      - realtime JSON tree (REST)
* ``/v1/auth/signup`` ``/v1/auth/signin`` ``/v1/auth/verify`` - local auth
* ``/v1/functions/<name>``                    - invoke an onRequest function
* ``/__health``                               - liveness probe
* anything else                               - static hosting (if enabled)

The server is std-lib only (``http.server`` + ``ThreadingHTTPServer``) and can
run in-process for tests via :func:`make_server`.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse, parse_qs

from .auth import AuthService, AuthError
from .firestore import Firestore
from .functions import (FunctionRegistry, ON_CREATE, ON_UPDATE, ON_DELETE)
from .hosting import Hosting
from .rtdb import RealtimeDatabase
from .storage import make_store


class App:
    """Container wiring the service instances over a shared store."""

    def __init__(self, data_dir: Optional[str] = None,
                 public_dir: Optional[str] = None,
                 secret: Optional[str] = None,
                 spa_fallback: bool = False) -> None:
        self.store = make_store(data_dir)
        self.firestore = Firestore(self.store)
        self.rtdb = RealtimeDatabase(self.store)
        self.auth = AuthService(self.store, secret=secret)
        self.functions = FunctionRegistry()
        self.hosting = Hosting(public_dir, spa_fallback=spa_fallback) \
            if public_dir else None


def _make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "openfirebase/0.1"
        protocol_version = "HTTP/1.1"

        # ---- helpers ------------------------------------------------------
        def log_message(self, *args):  # silence default logging in tests
            pass

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, data: bytes, ctype: str, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        # ---- verb entry points -------------------------------------------
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

        # ---- router -------------------------------------------------------
        def _route(self, method):
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
            except (ValueError, json.JSONDecodeError):
                return self._send_json({"error": "invalid JSON body"}, 400)
            query = parse_qs(parsed.query)

            try:
                if path == "/__health":
                    return self._send_json({"status": "ok", "service": "openfirebase"})
                if path.startswith("/v1/firestore"):
                    return self._firestore(method, path, body)
                if path.startswith("/v1/rtdb"):
                    return self._rtdb(method, path, body)
                if path.startswith("/v1/auth"):
                    return self._auth(method, path, body)
                if path.startswith("/v1/functions"):
                    return self._functions(method, path, body, query)
                return self._static(path)
            except AuthError as exc:
                return self._send_json({"error": str(exc)}, 401)
            except Exception as exc:  # pragma: no cover - defensive
                return self._send_json({"error": str(exc)}, 500)

        # ---- firestore ----------------------------------------------------
        def _firestore(self, method, path, body):
            parts = [p for p in path[len("/v1/firestore"):].split("/") if p]
            if not parts:
                return self._send_json({"error": "collection required"}, 400)
            collection = parts[0]
            doc_id = parts[1] if len(parts) > 1 else None
            fs = app.firestore
            if method == "GET":
                if doc_id:
                    doc = fs.get(collection, doc_id)
                    if doc is None:
                        return self._send_json({"error": "not found"}, 404)
                    return self._send_json(doc)
                return self._send_json({"documents": fs.list(collection)})
            if method == "POST":
                new_id = fs.add(collection, body)
                app.functions.dispatch_db(ON_CREATE, f"{collection}/{new_id}",
                                          None, body)
                return self._send_json({"id": new_id}, 201)
            if method in ("PUT", "PATCH"):
                if not doc_id:
                    return self._send_json({"error": "doc id required"}, 400)
                existed = fs.exists(collection, doc_id)
                if method == "PATCH":
                    ok = fs.update(collection, doc_id, body)
                    if not ok:
                        return self._send_json({"error": "not found"}, 404)
                else:
                    fs.set(collection, doc_id, body)
                event = ON_UPDATE if existed else ON_CREATE
                app.functions.dispatch_db(event, f"{collection}/{doc_id}",
                                          None, body)
                return self._send_json({"id": doc_id})
            if method == "DELETE":
                if not doc_id:
                    return self._send_json({"error": "doc id required"}, 400)
                ok = fs.delete(collection, doc_id)
                if ok:
                    app.functions.dispatch_db(ON_DELETE, f"{collection}/{doc_id}",
                                              None, None)
                return self._send_json({"deleted": ok})
            return self._send_json({"error": "method not allowed"}, 405)

        # ---- rtdb ---------------------------------------------------------
        def _rtdb(self, method, path, body):
            db_path = path[len("/v1/rtdb"):] or "/"
            db = app.rtdb
            if method == "GET":
                return self._send_json({"path": db_path, "value": db.get(db_path)})
            if method == "PUT":
                value = body.get("value") if isinstance(body, dict) and "value" in body \
                    else body
                db.set(db_path, value)
                app.functions.dispatch_db(ON_UPDATE, db_path.strip("/"), None, value)
                return self._send_json({"path": db_path, "value": db.get(db_path)})
            if method == "PATCH":
                db.update(db_path, body if isinstance(body, dict) else {})
                return self._send_json({"path": db_path, "value": db.get(db_path)})
            if method == "POST":
                value = body.get("value") if isinstance(body, dict) and "value" in body \
                    else body
                key = db.push(db_path, value)
                app.functions.dispatch_db(ON_CREATE, f"{db_path.strip('/')}/{key}",
                                          None, value)
                return self._send_json({"key": key}, 201)
            if method == "DELETE":
                ok = db.delete(db_path)
                return self._send_json({"deleted": ok})
            return self._send_json({"error": "method not allowed"}, 405)

        # ---- auth ---------------------------------------------------------
        def _auth(self, method, path, body):
            action = path[len("/v1/auth"):].strip("/")
            auth = app.auth
            if action == "signup" and method == "POST":
                user = auth.sign_up(body.get("email", ""), body.get("password", ""),
                                    body.get("display_name"))
                token = auth.issue_token(user["uid"])
                return self._send_json({"user": user, "id_token": token}, 201)
            if action == "signin" and method == "POST":
                return self._send_json(auth.sign_in(body.get("email", ""),
                                                    body.get("password", "")))
            if action == "verify" and method == "POST":
                payload = auth.verify_token(body.get("id_token", ""))
                return self._send_json({"valid": True, "claims": payload})
            return self._send_json({"error": "unknown auth action"}, 404)

        # ---- functions ----------------------------------------------------
        def _functions(self, method, path, body, query):
            name = path[len("/v1/functions"):].strip("/")
            if not name:
                return self._send_json(
                    {"http": app.functions.list_http_handlers(),
                     "db": app.functions.list_db_handlers()})
            try:
                request = {"method": method, "body": body, "query": query}
                result = app.functions.call_request(name, request)
            except KeyError:
                return self._send_json({"error": f"no function {name!r}"}, 404)
            return self._send_json({"result": result})

        # ---- static hosting ----------------------------------------------
        def _static(self, path):
            if app.hosting is None:
                return self._send_json({"error": "not found"}, 404)
            served = app.hosting.serve(path)
            if served is None:
                return self._send_json({"error": "not found"}, 404)
            data, ctype = served
            return self._send_bytes(data, ctype)

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 8080,
                app: Optional[App] = None, **app_kwargs) -> ThreadingHTTPServer:
    """Create (but do not start) a ThreadingHTTPServer bound to ``host:port``."""
    if app is None:
        app = App(**app_kwargs)
    httpd = ThreadingHTTPServer((host, port), _make_handler(app))
    httpd.openfirebase_app = app  # type: ignore[attr-defined]
    return httpd


def serve_forever(host: str = "127.0.0.1", port: int = 8080,
                  app: Optional[App] = None, **app_kwargs) -> None:
    httpd = make_server(host, port, app, **app_kwargs)
    print(f"openfirebase listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.shutdown()


def run_in_thread(host: str = "127.0.0.1", port: int = 0,
                  app: Optional[App] = None, **app_kwargs):
    """Start a server on a background thread; returns ``(httpd, thread, port)``."""
    httpd = make_server(host, port, app, **app_kwargs)
    actual_port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, actual_port
