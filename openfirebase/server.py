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
* ``/v1/auth/signup``                        - create user
* ``/v1/auth/signin``                        - sign in
* ``/v1/auth/verify``                        - verify id-token
* ``/v1/auth/users``                         - list users (GET) / get user (GET /<uid>)
* ``/v1/auth/users/<uid>``                   - update (PATCH) / delete (DELETE) user
* ``/v1/auth/custom-token``                  - mint custom token (POST)
* ``/v1/auth/password-reset``                - generate/confirm password reset
* ``/v1/auth/email-verification``            - generate/confirm email verification
* ``/v1/auth/provider-signin``               - provider sign-in stub (POST)
* ``/v1/auth/set-custom-claims``             - set custom claims (POST)
* ``/v1/functions/<name>``                    - invoke an onRequest function
* ``/v1/functions/_callable/<name>``          - invoke a callable function
* ``/v1/functions/_pubsub/<topic>``           - publish a Pub/Sub message (POST)
* ``/v1/functions/_schedule/<name>``          - run a scheduled function (POST)
* ``/v1/storage/<bucket>/o``                  - list objects
* ``/v1/storage/<bucket>/o/<name>``           - upload/download/delete object
* ``/v1/storage/<bucket>/o/<name>/meta``      - get/patch object metadata
* ``/v1/storage/<bucket>/o/<name>/token``     - rotate download token
* ``/v1/storage``                             - list buckets
* ``/v1/hosting/channels``                    - list preview channels (GET)
* ``/v1/hosting/channels/<name>``             - create/delete preview channel
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
from .functions import (FunctionRegistry, ON_CREATE, ON_UPDATE, ON_DELETE,
                        ON_STORAGE_FINALIZE, ON_STORAGE_DELETE,
                        ON_AUTH_USER_CREATE, ON_AUTH_USER_DELETE)
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
                if path.startswith("/v1/hosting"):
                    body = self._safe_json_body(method)
                    return self._hosting_mgmt(method, path, body)
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
            sub = path[len("/v1/auth"):].lstrip("/")
            # split off leading segment for routing, keeping rest as tail
            parts = sub.split("/", 1)
            action = parts[0]
            tail = parts[1] if len(parts) > 1 else ""
            auth = app.auth

            # ---- basic sign-up/sign-in/verify (unchanged) ---
            if action == "signup" and method == "POST":
                user = auth.sign_up(body.get("email", ""),
                                    body.get("password", ""),
                                    body.get("display_name"))
                token = auth.issue_token(user["uid"])
                app.functions.dispatch_auth(ON_AUTH_USER_CREATE, dict(user))
                return self._send_json({"user": user, "id_token": token}, 201)

            if action == "signin" and method == "POST":
                return self._send_json(auth.sign_in(body.get("email", ""),
                                                    body.get("password", "")))

            if action == "verify" and method == "POST":
                payload = auth.verify_token(body.get("id_token", ""))
                return self._send_json({"valid": True, "claims": payload})

            # ---- custom-token ---
            if action == "custom-token" and method == "POST":
                uid = body.get("uid", "")
                if not uid:
                    return self._send_json({"error": "uid required"}, 400)
                token = auth.mint_custom_token(
                    uid,
                    custom_claims=body.get("custom_claims"),
                    ttl=body.get("ttl"),
                )
                return self._send_json({"custom_token": token})

            if action == "verify-custom-token" and method == "POST":
                try:
                    payload = auth.verify_custom_token(body.get("token", ""))
                    return self._send_json({"valid": True, "claims": payload})
                except AuthError as exc:
                    return self._send_json({"error": str(exc)}, 401)

            # ---- user CRUD ---
            if action == "users":
                if method == "GET" and not tail:
                    page_size = int(body.get("page_size", 100)) \
                        if isinstance(body, dict) else 100
                    page_token = (body.get("page_token") or None) \
                        if isinstance(body, dict) else None
                    return self._send_json(auth.list_users(page_size, page_token))
                if tail:
                    uid = tail
                    if method == "GET":
                        user = auth.get_user(uid)
                        if user is None:
                            return self._send_json({"error": "not found"}, 404)
                        return self._send_json(user)
                    if method == "PATCH":
                        try:
                            user = auth.update_user(uid, **{
                                k: v for k, v in body.items()
                                if k in {"email", "password", "display_name",
                                         "disabled", "email_verified",
                                         "custom_claims"}
                            })
                            return self._send_json(user)
                        except AuthError as exc:
                            return self._send_json({"error": str(exc)}, 400)
                    if method == "DELETE":
                        user = auth.get_user(uid)
                        ok = auth.delete_user(uid)
                        if ok and user:
                            app.functions.dispatch_auth(ON_AUTH_USER_DELETE, dict(user))
                        return self._send_json({"deleted": ok})
                return self._send_json({"error": "method not allowed"}, 405)

            # ---- password-reset flow ---
            if action == "password-reset":
                if method == "POST":
                    sub_action = body.get("action", "generate")
                    if sub_action == "generate":
                        email = body.get("email", "")
                        try:
                            token = auth.generate_password_reset_token(email)
                            return self._send_json({"reset_token": token})
                        except AuthError as exc:
                            return self._send_json({"error": str(exc)}, 400)
                    if sub_action == "confirm":
                        try:
                            auth.confirm_password_reset(
                                body.get("reset_token", ""),
                                body.get("new_password", ""),
                            )
                            return self._send_json({"status": "ok"})
                        except AuthError as exc:
                            return self._send_json({"error": str(exc)}, 400)
                return self._send_json({"error": "method not allowed"}, 405)

            # ---- email-verification flow ---
            if action == "email-verification":
                if method == "POST":
                    sub_action = body.get("action", "generate")
                    if sub_action == "generate":
                        uid = body.get("uid", "")
                        try:
                            token = auth.generate_email_verification_token(uid)
                            return self._send_json({"verification_token": token})
                        except AuthError as exc:
                            return self._send_json({"error": str(exc)}, 400)
                    if sub_action == "confirm":
                        try:
                            user = auth.confirm_email_verification(
                                body.get("verification_token", ""))
                            return self._send_json(user)
                        except AuthError as exc:
                            return self._send_json({"error": str(exc)}, 400)
                return self._send_json({"error": "method not allowed"}, 405)

            # ---- provider sign-in stub ---
            if action == "provider-signin" and method == "POST":
                try:
                    result = auth.sign_in_with_provider(
                        body.get("provider_id", ""),
                        body.get("provider_uid", ""),
                        email=body.get("email"),
                        display_name=body.get("display_name"),
                    )
                    return self._send_json(result)
                except AuthError as exc:
                    return self._send_json({"error": str(exc)}, 400)

            # ---- custom claims ---
            if action == "set-custom-claims" and method == "POST":
                uid = body.get("uid", "")
                claims = body.get("custom_claims", {})
                try:
                    user = auth.set_custom_claims(uid, claims)
                    return self._send_json(user)
                except AuthError as exc:
                    return self._send_json({"error": str(exc)}, 400)

            return self._send_json({"error": "unknown auth action"}, 404)

        # ---- functions ----------------------------------------------------
        def _functions(self, method, path, body, query):
            sub = path[len("/v1/functions"):].lstrip("/")

            # Listing endpoint
            if not sub:
                return self._send_json({
                    "http": app.functions.list_http_handlers(),
                    "callable": app.functions.list_callable_handlers(),
                    "db": app.functions.list_db_handlers(),
                    "auth": app.functions.list_auth_handlers(),
                    "storage": app.functions.list_storage_handlers(),
                    "pubsub": app.functions.list_pubsub_handlers(),
                    "scheduled": app.functions.list_scheduled(),
                })

            # Callable endpoint: POST /v1/functions/_callable/<name>
            if sub.startswith("_callable/"):
                name = sub[len("_callable/"):]
                if method != "POST":
                    return self._send_json({"error": "method not allowed"}, 405)
                try:
                    result = app.functions.call_callable(
                        name,
                        body.get("data") if isinstance(body, dict) else body,
                        body.get("context") if isinstance(body, dict) else None,
                    )
                    return self._send_json(result)
                except KeyError:
                    return self._send_json({"error": f"no callable {name!r}"}, 404)

            # Pub/Sub publish: POST /v1/functions/_pubsub/<topic>
            if sub.startswith("_pubsub/"):
                topic = sub[len("_pubsub/"):]
                if method != "POST":
                    return self._send_json({"error": "method not allowed"}, 405)
                message = body.get("message") if isinstance(body, dict) else body
                results = app.functions.publish(topic, message)
                return self._send_json({"topic": topic, "results": results,
                                        "count": len(results)})

            # Scheduled run: POST /v1/functions/_schedule/<name>
            if sub.startswith("_schedule/"):
                name = sub[len("_schedule/"):]
                if method != "POST":
                    return self._send_json({"error": "method not allowed"}, 405)
                try:
                    result = app.functions.run_scheduled(name)
                    return self._send_json({"result": result})
                except KeyError:
                    return self._send_json({"error": f"no scheduled {name!r}"}, 404)

            # Default: onRequest invocation
            name = sub
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
                app.functions.dispatch_storage(ON_STORAGE_FINALIZE, dict(meta))
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
                try:
                    meta_before = cs.get_metadata(bucket, obj_name)
                except ObjectNotFoundError:
                    meta_before = None
                ok = cs.delete(bucket, obj_name)
                if ok and meta_before:
                    app.functions.dispatch_storage(ON_STORAGE_DELETE, dict(meta_before))
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

        # ---- static hosting (file serving) ----------------------------------
        def _static(self, path):
            if app.hosting is None:
                return self._send_json({"error": "not found"}, 404)
            # Check redirect rules first
            redir = app.hosting.check_redirect(path)
            if redir is not None:
                self.send_response(redir["status"])
                self.send_header("Location", redir["destination"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Check rewrite → function stub
            rewrite = app.hosting.check_rewrite(path)
            if rewrite and "function" in rewrite:
                # Attempt to call the function; fall through to 404 if missing
                fn_name = rewrite["function"]
                try:
                    result = app.functions.call_request(
                        fn_name,
                        {"method": "GET", "body": {}, "query": {}, "path": path},
                    )
                    return self._send_json({"result": result})
                except KeyError:
                    return self._send_json(
                        {"error": f"function {fn_name!r} not registered"}, 404)
            served = app.hosting.serve_with_headers(path)
            if served is None:
                return self._send_json({"error": "not found"}, 404)
            data, ctype, extra = served
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        # ---- hosting management (channels / config) -----------------------
        def _hosting_mgmt(self, method, path, body):
            if app.hosting is None:
                return self._send_json({"error": "hosting not configured"}, 400)
            sub = path[len("/v1/hosting"):].lstrip("/")

            if sub == "channels" or sub == "channels/":
                if method == "GET":
                    return self._send_json({
                        "channels": app.hosting.list_channels()
                    })
                return self._send_json({"error": "method not allowed"}, 405)

            if sub.startswith("channels/"):
                channel_name = sub[len("channels/"):]
                if method == "POST":
                    overlay = (body.get("dir") if isinstance(body, dict) else None)
                    app.hosting.create_channel(channel_name, overlay)
                    return self._send_json({
                        "name": channel_name,
                        "url": app.hosting.get_channel_url(channel_name),
                    }, 201)
                if method == "DELETE":
                    ok = app.hosting.delete_channel(channel_name)
                    return self._send_json({"deleted": ok})
                return self._send_json({"error": "method not allowed"}, 405)

            return self._send_json({"error": "unknown hosting endpoint"}, 404)

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
