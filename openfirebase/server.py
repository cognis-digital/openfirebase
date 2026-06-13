"""Single local HTTP server exposing all openfirebase services.

Path prefixes
-------------
* ``/v1/firestore/<collection>[/<doc_id>]``  - document database (REST)
* ``/v1/firestore/<col>/<doc>/~/<subcol>[/<subdoc>]`` - subcollections
* ``/v1/firestore/_query/<collection>``      - POST queries with filters/order
* ``/v1/firestore/_batch``                   - batched writes (POST)
* ``/v1/firestore/_transaction/<collection>``- transactions (POST)
* ``/v1/rtdb/<path...>``                      - realtime JSON tree (REST)
* ``/v1/rtdb/_query/<path...>``              - RTDB query (GET)
* ``/v1/rtdb/_transaction/<path...>``        - RTDB transaction (POST)
* ``/v1/auth/signup`` ``/v1/auth/signin`` ``/v1/auth/verify`` - local auth
* ``/v1/functions/<name>``                    - invoke an onRequest function
* ``/v1/storage/<bucket>/o``                  - list objects
* ``/v1/storage/<bucket>/o/<name>``           - upload/download/delete object
* ``/v1/storage/<bucket>/o/<name>/meta``      - get/patch object metadata
* ``/v1/storage/<bucket>/o/<name>/token``     - rotate download token
* ``/v1/storage``                             - list buckets
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
from .cloudstorage import CloudStorage, ObjectNotFoundError
from .firestore import Firestore, FieldValue, TransactionError
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
        self.cloud_storage = CloudStorage(self.store)


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
            return raw  # return raw bytes for storage uploads

        def _read_json_body(self):
            raw = self._read_body()
            if isinstance(raw, dict):
                return raw
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                raise

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
            query = parse_qs(parsed.query)

            try:
                if path == "/__health":
                    return self._send_json({"status": "ok", "service": "openfirebase"})
                if path.startswith("/v1/firestore"):
                    return self._firestore_route(method, path, query)
                if path.startswith("/v1/rtdb"):
                    return self._rtdb_route(method, path, query)
                if path.startswith("/v1/auth"):
                    body = self._safe_json_body(method)
                    return self._auth(method, path, body)
                if path.startswith("/v1/functions"):
                    body = self._safe_json_body(method)
                    return self._functions(method, path, body, query)
                if path.startswith("/v1/storage"):
                    return self._storage_route(method, path, query)
                return self._static(path)
            except AuthError as exc:
                return self._send_json({"error": str(exc)}, 401)
            except Exception as exc:  # pragma: no cover - defensive
                return self._send_json({"error": str(exc)}, 500)

        def _safe_json_body(self, method):
            if method not in ("POST", "PUT", "PATCH"):
                return {}
            try:
                return self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                return {}

        # ---- firestore ---------------------------------------------------
        def _firestore_route(self, method, path, query):
            sub = path[len("/v1/firestore"):]

            # batched writes: POST /v1/firestore/_batch
            if sub == "/_batch" or sub == "/_batch/":
                if method != "POST":
                    return self._send_json({"error": "method not allowed"}, 405)
                try:
                    body = self._read_json_body()
                except (ValueError, json.JSONDecodeError):
                    return self._send_json({"error": "invalid JSON body"}, 400)
                return self._firestore_batch(body)

            # query endpoint: POST /v1/firestore/_query/<collection>
            if sub.startswith("/_query/"):
                col_path = sub[len("/_query/"):]
                if method != "POST":
                    return self._send_json({"error": "method not allowed"}, 405)
                try:
                    body = self._read_json_body()
                except (ValueError, json.JSONDecodeError):
                    return self._send_json({"error": "invalid JSON body"}, 400)
                return self._firestore_query(col_path, body)

            # transaction: POST /v1/firestore/_transaction
            if sub.startswith("/_transaction"):
                if method != "POST":
                    return self._send_json({"error": "method not allowed"}, 405)
                try:
                    body = self._read_json_body()
                except (ValueError, json.JSONDecodeError):
                    return self._send_json({"error": "invalid JSON body"}, 400)
                return self._firestore_transaction(body)

            # regular CRUD — support subcollections via /col/doc/~/subcol
            try:
                body = self._safe_json_body(method)
                if isinstance(body, bytes):
                    body = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, json.JSONDecodeError):
                return self._send_json({"error": "invalid JSON body"}, 400)
            return self._firestore(method, path, body)

        def _firestore(self, method, path, body):
            raw_parts = [p for p in path[len("/v1/firestore"):].split("/") if p]
            if not raw_parts:
                return self._send_json({"error": "collection required"}, 400)

            # Support subcollections: col/doc/~/subcol/subdoc
            # The tilde (~) is used as a separator for subcollection paths in URLs
            if "~" in raw_parts:
                tilde_idx = raw_parts.index("~")
                parent_path = "/".join(raw_parts[:tilde_idx])
                sub_parts = raw_parts[tilde_idx + 1:]
                if not sub_parts:
                    return self._send_json({"error": "subcollection required"}, 400)
                collection = f"{parent_path}/{sub_parts[0]}"
                doc_id = sub_parts[1] if len(sub_parts) > 1 else None
            else:
                collection = raw_parts[0]
                doc_id = raw_parts[1] if len(raw_parts) > 1 else None

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

        def _firestore_query(self, collection: str, body: dict):
            """Handle POST /v1/firestore/_query/<collection>.

            Body schema::

                {
                    "where":    [{"field": str, "op": str, "value": any}, ...],
                    "order_by": [{"field": str, "direction": "asc"|"desc"}, ...],
                    "limit":    int,
                    "start_after": <doc snapshot or field value>,
                    "start_at":    <doc snapshot or field value>,
                    "end_before":  <doc snapshot or field value>,
                    "end_at":      <doc snapshot or field value>,
                }
            """
            q = app.firestore.collection(collection)
            for f in body.get("where", []):
                q = q.where(f["field"], f["op"], f["value"])
            for o in body.get("order_by", []):
                q = q.order_by(o["field"], o.get("direction", "asc"))
            if "limit" in body:
                q = q.limit(int(body["limit"]))
            if "start_after" in body:
                q = q.start_after(body["start_after"])
            if "start_at" in body:
                q = q.start_at(body["start_at"])
            if "end_before" in body:
                q = q.end_before(body["end_before"])
            if "end_at" in body:
                q = q.end_at(body["end_at"])
            return self._send_json({"documents": q.stream()})

        def _firestore_batch(self, body: dict):
            """Handle POST /v1/firestore/_batch.

            Body schema::

                {
                    "writes": [
                        {"op": "set",    "collection": str, "id": str, "data": dict, "merge": bool},
                        {"op": "update", "collection": str, "id": str, "data": dict},
                        {"op": "delete", "collection": str, "id": str},
                    ]
                }
            """
            batch = app.firestore.batch()
            for write in body.get("writes", []):
                op = write.get("op")
                col = write.get("collection", "")
                doc_id = write.get("id", "")
                if op == "set":
                    batch.set(col, doc_id, write.get("data", {}),
                              merge=bool(write.get("merge", False)))
                elif op == "update":
                    batch.update(col, doc_id, write.get("data", {}))
                elif op == "delete":
                    batch.delete(col, doc_id)
                else:
                    return self._send_json({"error": f"unknown op: {op!r}"}, 400)
            batch.commit()
            return self._send_json({"status": "ok", "count": len(body.get("writes", []))})

        def _firestore_transaction(self, body: dict):
            """Handle POST /v1/firestore/_transaction.

            Body schema::

                {
                    "writes": [
                        {"op": "set",    "collection": str, "id": str, "data": dict},
                        {"op": "update", "collection": str, "id": str, "data": dict},
                        {"op": "delete", "collection": str, "id": str},
                    ]
                }

            A transaction without reads is equivalent to a batched write but
            with the retry-on-conflict semantics. The HTTP endpoint is a
            simplified form — full read-modify-write transactions require
            in-process use of ``Firestore.run_transaction``.
            """
            writes = body.get("writes", [])
            try:
                def txn_fn(txn):
                    for write in writes:
                        op = write.get("op")
                        col = write.get("collection", "")
                        doc_id = write.get("id", "")
                        if op == "set":
                            txn.set(col, doc_id, write.get("data", {}),
                                    merge=bool(write.get("merge", False)))
                        elif op == "update":
                            txn.update(col, doc_id, write.get("data", {}))
                        elif op == "delete":
                            txn.delete(col, doc_id)
                app.firestore.run_transaction(txn_fn)
            except TransactionError as exc:
                return self._send_json({"error": str(exc)}, 409)
            return self._send_json({"status": "ok", "count": len(writes)})

        # ---- rtdb ---------------------------------------------------------
        def _rtdb_route(self, method, path, query):
            # RTDB query endpoint: GET /v1/rtdb/_query/<path>
            if "/v1/rtdb/_query" in path:
                db_path = path[len("/v1/rtdb/_query"):] or "/"
                return self._rtdb_query(db_path, query)

            # RTDB transaction: POST /v1/rtdb/_transaction/<path>
            if "/v1/rtdb/_transaction" in path:
                db_path = path[len("/v1/rtdb/_transaction"):] or "/"
                if method != "POST":
                    return self._send_json({"error": "method not allowed"}, 405)
                try:
                    body = self._read_json_body()
                except (ValueError, json.JSONDecodeError):
                    return self._send_json({"error": "invalid JSON body"}, 400)
                return self._rtdb_transaction(db_path, body)

            # regular RTDB CRUD
            try:
                body = self._safe_json_body(method)
                if isinstance(body, bytes):
                    body = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, json.JSONDecodeError):
                return self._send_json({"error": "invalid JSON body"}, 400)
            db_path = path[len("/v1/rtdb"):] or "/"
            return self._rtdb(method, db_path, body)

        def _rtdb(self, method, db_path, body):
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

        def _rtdb_query(self, db_path: str, qs: dict):
            """Handle GET /v1/rtdb/_query/<path>.

            Query params:
            - ``orderByChild``  — child field name
            - ``orderByKey``    — use "$key"
            - ``equalTo``       — exact match value (JSON-decoded)
            - ``startAt``       — range start (JSON-decoded)
            - ``endAt``         — range end (JSON-decoded)
            - ``limitToFirst``  — int
            - ``limitToLast``   — int
            """
            q = app.rtdb.query(db_path)
            if "orderByChild" in qs:
                q = q.order_by_child(qs["orderByChild"][0])
            elif "orderByKey" in qs:
                q = q.order_by_key()
            elif "orderByValue" in qs:
                q = q.order_by_value()
            if "equalTo" in qs:
                q = q.equal_to(_json_decode(qs["equalTo"][0]))
            if "startAt" in qs:
                q = q.start_at(_json_decode(qs["startAt"][0]))
            if "endAt" in qs:
                q = q.end_at(_json_decode(qs["endAt"][0]))
            if "limitToFirst" in qs:
                q = q.limit_to_first(int(qs["limitToFirst"][0]))
            if "limitToLast" in qs:
                q = q.limit_to_last(int(qs["limitToLast"][0]))
            return self._send_json({"path": db_path, "results": q.get()})

        def _rtdb_transaction(self, db_path: str, body: dict):
            """Handle POST /v1/rtdb/_transaction/<path>.

            Body: ``{"op": "increment", "value": <number>}``
            or   ``{"op": "set_if_null", "value": <any>}``

            Applies the operation atomically via ``rtdb.transaction``.
            """
            op = body.get("op", "increment")
            value = body.get("value", 1)

            if op == "increment":
                def updater(current):
                    return (current or 0) + value
            elif op == "set_if_null":
                def updater(current):
                    return current if current is not None else value
            elif op == "set":
                def updater(current):
                    return value
            else:
                return self._send_json({"error": f"unknown op: {op!r}"}, 400)

            result = app.rtdb.transaction(db_path, updater)
            return self._send_json({"path": db_path, "value": result})

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

        # ---- cloud storage -----------------------------------------------
        def _storage_route(self, method, path, query):
            sub = path[len("/v1/storage"):]

            # GET /v1/storage — list buckets
            if not sub or sub == "/":
                if method != "GET":
                    return self._send_json({"error": "method not allowed"}, 405)
                return self._send_json({"buckets": app.cloud_storage.list_buckets()})

            parts = [p for p in sub.split("/") if p]
            if not parts:
                return self._send_json({"error": "bucket required"}, 400)

            bucket = parts[0]

            # /v1/storage/<bucket>/o  — list objects
            if len(parts) == 2 and parts[1] == "o":
                if method != "GET":
                    return self._send_json({"error": "method not allowed"}, 405)
                prefix = query.get("prefix", [""])[0]
                return self._send_json({
                    "bucket": bucket,
                    "objects": app.cloud_storage.list_objects(bucket, prefix=prefix)
                })

            if len(parts) >= 3 and parts[1] == "o":
                # object name may contain slashes — re-join from index 2
                obj_name = "/".join(parts[2:])

                # strip trailing /meta or /token
                if obj_name.endswith("/meta"):
                    obj_name = obj_name[:-len("/meta")]
                    return self._storage_meta(method, bucket, obj_name)

                if obj_name.endswith("/token"):
                    obj_name = obj_name[:-len("/token")]
                    if method != "POST":
                        return self._send_json({"error": "method not allowed"}, 405)
                    try:
                        token = app.cloud_storage.rotate_token(bucket, obj_name)
                        return self._send_json({"token": token})
                    except ObjectNotFoundError:
                        return self._send_json({"error": "not found"}, 404)

                return self._storage_object(method, bucket, obj_name, query)

            return self._send_json({"error": "invalid storage path"}, 400)

        def _storage_object(self, method, bucket, obj_name, query):
            cs = app.cloud_storage
            if method == "POST" or method == "PUT":
                # read raw bytes
                raw = self._read_body()
                req_ctype = self.headers.get("Content-Type", "")
                if "application/json" in req_ctype:
                    # JSON envelope upload: {"base64_data": "...", "content_type": "...", ...}
                    import base64 as _b64
                    try:
                        payload = json.loads(raw.decode("utf-8")) if raw else {}
                    except (ValueError, json.JSONDecodeError):
                        return self._send_json({"error": "invalid JSON body"}, 400)
                    b64 = payload.get("base64_data", "")
                    data = _b64.b64decode(b64) if b64 else b""
                    ctype = payload.get("content_type", "application/octet-stream")
                    custom = payload.get("custom_metadata", {})
                else:
                    # raw binary upload
                    data = raw if isinstance(raw, (bytes, bytearray)) else b""
                    ctype = req_ctype or "application/octet-stream"
                    custom = {}
                if not isinstance(data, (bytes, bytearray)):
                    data = b""
                meta = cs.upload(bucket, obj_name, data, ctype, custom_metadata=custom)
                return self._send_json(meta, 201)

            if method == "GET":
                # check token if provided
                try:
                    data = cs.download(bucket, obj_name)
                    meta = cs.get_metadata(bucket, obj_name)
                except ObjectNotFoundError:
                    return self._send_json({"error": "not found"}, 404)
                ctype = meta.get("content_type", "application/octet-stream")
                return self._send_bytes(data, ctype)

            if method == "DELETE":
                ok = cs.delete(bucket, obj_name)
                return self._send_json({"deleted": ok})

            return self._send_json({"error": "method not allowed"}, 405)

        def _storage_meta(self, method, bucket, obj_name):
            cs = app.cloud_storage
            if method == "GET":
                try:
                    meta = cs.get_metadata(bucket, obj_name)
                    return self._send_json(meta)
                except ObjectNotFoundError:
                    return self._send_json({"error": "not found"}, 404)
            if method == "PATCH":
                try:
                    body = self._read_json_body()
                except (ValueError, json.JSONDecodeError):
                    return self._send_json({"error": "invalid JSON body"}, 400)
                try:
                    meta = cs.update_metadata(bucket, obj_name,
                                              body.get("custom_metadata", {}))
                    return self._send_json(meta)
                except ObjectNotFoundError:
                    return self._send_json({"error": "not found"}, 404)
            return self._send_json({"error": "method not allowed"}, 405)

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


def _json_decode(s: str):
    """Decode a JSON-encoded query param value, falling back to raw string."""
    try:
        return json.loads(s)
    except (ValueError, json.JSONDecodeError):
        return s


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
