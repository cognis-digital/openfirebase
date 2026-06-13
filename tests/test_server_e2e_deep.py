"""End-to-end tests for the new services and endpoints added in the
storage+data pass:

- Firestore: /_query  (POST query with filters/order/limit/cursors)
- Firestore: /_batch  (batched writes)
- Firestore: /_transaction (server-side transaction)
- Firestore: subcollections via /col/doc/~/subcol
- RTDB: /_query  (orderByChild / equalTo / limitToFirst)
- RTDB: /_transaction  (increment / set_if_null)
- Cloud Storage: upload / download / list / metadata / token-rotation / delete
"""

import base64
import json
import urllib.request
import urllib.error

import pytest

from openfirebase.server import App, run_in_thread


@pytest.fixture(scope="module")
def server():
    app = App(data_dir=None, secret="e2e-deep-secret")
    httpd, thread, port = run_in_thread(port=0, app=app)
    yield {"port": port, "app": app}
    httpd.shutdown()


def _req(port, method, path, body=None, raw_bytes=None, content_type=None):
    url = f"http://127.0.0.1:{port}{path}"
    if raw_bytes is not None:
        data = raw_bytes
        ctype = content_type or "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode()
        ctype = "application/json"
    else:
        data = None
        ctype = None
    req = urllib.request.Request(url, data=data, method=method)
    if ctype and data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def _req_bytes(port, method, path):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------------------
# Firestore: advanced query via POST /_query/<collection>
# ---------------------------------------------------------------------------

def test_firestore_query_with_where_and_order(server):
    port = server["port"]
    # seed docs
    for name, price in [("apple", 3), ("banana", 1), ("carrot", 2)]:
        _req(port, "POST", "/v1/firestore/fruits", {"name": name, "price": price})
    status, body = _req(port, "POST", "/v1/firestore/_query/fruits", {
        "where":    [{"field": "price", "op": ">", "value": 1}],
        "order_by": [{"field": "price", "direction": "asc"}],
        "limit":    2,
    })
    assert status == 200
    docs = body["documents"]
    assert len(docs) == 2
    assert docs[0]["price"] < docs[1]["price"]
    assert all(d["price"] > 1 for d in docs)


def test_firestore_query_cursor_start_at(server):
    port = server["port"]
    # use existing fruits from previous test
    status, body = _req(port, "POST", "/v1/firestore/_query/fruits", {
        "order_by": [{"field": "price", "direction": "asc"}],
        "start_at": 2,
    })
    assert status == 200
    assert all(d["price"] >= 2 for d in body["documents"])


def test_firestore_query_wrong_method(server):
    port = server["port"]
    status, _ = _req(port, "GET", "/v1/firestore/_query/fruits")
    assert status == 405


# ---------------------------------------------------------------------------
# Firestore: batched writes
# ---------------------------------------------------------------------------

def test_firestore_batch_commit(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/firestore/_batch", {
        "writes": [
            {"op": "set",    "collection": "batch_col", "id": "d1", "data": {"v": 1}},
            {"op": "set",    "collection": "batch_col", "id": "d2", "data": {"v": 2}},
            {"op": "update", "collection": "batch_col", "id": "d1", "data": {"v": 10}},
        ]
    })
    assert status == 200 and body["status"] == "ok" and body["count"] == 3
    _, d1 = _req(port, "GET", "/v1/firestore/batch_col/d1")
    _, d2 = _req(port, "GET", "/v1/firestore/batch_col/d2")
    assert d1["v"] == 10
    assert d2["v"] == 2


def test_firestore_batch_delete(server):
    port = server["port"]
    _req(port, "POST", "/v1/firestore/_batch", {
        "writes": [
            {"op": "set",    "collection": "batch_col2", "id": "x", "data": {"v": 1}},
            {"op": "delete", "collection": "batch_col2", "id": "x"},
        ]
    })
    status, _ = _req(port, "GET", "/v1/firestore/batch_col2/x")
    assert status == 404


def test_firestore_batch_unknown_op_returns_400(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/firestore/_batch", {
        "writes": [{"op": "merge", "collection": "c", "id": "d"}]
    })
    assert status == 400


# ---------------------------------------------------------------------------
# Firestore: transaction via HTTP
# ---------------------------------------------------------------------------

def test_firestore_transaction_writes(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/firestore/_transaction", {
        "writes": [
            {"op": "set",    "collection": "txn_col", "id": "t1", "data": {"val": 99}},
            {"op": "set",    "collection": "txn_col", "id": "t2", "data": {"val": 100}},
        ]
    })
    assert status == 200 and body["status"] == "ok"
    _, t1 = _req(port, "GET", "/v1/firestore/txn_col/t1")
    assert t1["val"] == 99


# ---------------------------------------------------------------------------
# Firestore: subcollections via /col/doc/~/subcol
# ---------------------------------------------------------------------------

def test_firestore_subcollection_crud(server):
    port = server["port"]
    # create parent doc
    _req(port, "PUT", "/v1/firestore/users/usr1", {"name": "Ada"})
    # create subcollection doc
    status, body = _req(port, "POST", "/v1/firestore/users/usr1/~/posts",
                        {"title": "Hello"})
    assert status == 201
    sub_id = body["id"]
    # retrieve it
    status, doc = _req(port, "GET", f"/v1/firestore/users/usr1/~/posts/{sub_id}")
    assert status == 200 and doc["title"] == "Hello"
    # list subcollection
    status, listing = _req(port, "GET", "/v1/firestore/users/usr1/~/posts")
    assert status == 200 and len(listing["documents"]) >= 1
    # delete it
    status, result = _req(port, "DELETE", f"/v1/firestore/users/usr1/~/posts/{sub_id}")
    assert status == 200 and result["deleted"] is True


# ---------------------------------------------------------------------------
# RTDB: query endpoint
# ---------------------------------------------------------------------------

def test_rtdb_query_order_by_child(server):
    port = server["port"]
    # seed data
    for k, score in [("a", 30), ("b", 10), ("c", 20)]:
        _req(port, "PUT", f"/v1/rtdb/scores/{k}", {"value": {"score": score}})
    status, body = _req(port, "GET",
        "/v1/rtdb/_query/scores?orderByChild=score&limitToFirst=2")
    assert status == 200
    results = body["results"]
    values = [v["score"] for v in results.values()]
    assert values == sorted(values)
    assert len(results) == 2


def test_rtdb_query_equal_to(server):
    port = server["port"]
    status, body = _req(port, "GET",
        "/v1/rtdb/_query/scores?orderByChild=score&equalTo=10")
    assert status == 200
    results = body["results"]
    assert all(v["score"] == 10 for v in results.values())


def test_rtdb_query_order_by_key(server):
    port = server["port"]
    status, body = _req(port, "GET",
        "/v1/rtdb/_query/scores?orderByKey=1&limitToFirst=2")
    assert status == 200
    keys = list(body["results"].keys())
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# RTDB: transaction endpoint
# ---------------------------------------------------------------------------

def test_rtdb_transaction_increment(server):
    port = server["port"]
    _req(port, "PUT", "/v1/rtdb/counters/visits", {"value": 0})
    status, body = _req(port, "POST", "/v1/rtdb/_transaction/counters/visits",
                        {"op": "increment", "value": 5})
    assert status == 200 and body["value"] == 5
    # increment again
    status, body = _req(port, "POST", "/v1/rtdb/_transaction/counters/visits",
                        {"op": "increment", "value": 3})
    assert body["value"] == 8


def test_rtdb_transaction_set_if_null(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/rtdb/_transaction/flags/new_flag",
                        {"op": "set_if_null", "value": True})
    assert status == 200 and body["value"] is True
    # second call should NOT overwrite since value is not null
    status, body = _req(port, "POST", "/v1/rtdb/_transaction/flags/new_flag",
                        {"op": "set_if_null", "value": False})
    assert body["value"] is True  # unchanged


def test_rtdb_transaction_set(server):
    port = server["port"]
    status, body = _req(port, "POST", "/v1/rtdb/_transaction/flags/overwrite",
                        {"op": "set", "value": "hello"})
    assert status == 200 and body["value"] == "hello"


def test_rtdb_transaction_unknown_op(server):
    port = server["port"]
    status, _ = _req(port, "POST", "/v1/rtdb/_transaction/x",
                     {"op": "unknown_op"})
    assert status == 400


# ---------------------------------------------------------------------------
# Cloud Storage: full round-trip via HTTP
# ---------------------------------------------------------------------------

def test_storage_list_buckets_empty(server):
    port = server["port"]
    # fresh app, so no buckets created yet
    status, body = _req(port, "GET", "/v1/storage")
    assert status == 200
    assert "buckets" in body


def test_storage_upload_via_base64(server):
    port = server["port"]
    data = b"hello storage"
    b64 = base64.b64encode(data).decode()
    status, body = _req(port, "POST", "/v1/storage/mybucket/o/hello.txt", {
        "base64_data": b64,
        "content_type": "text/plain",
    })
    assert status == 201
    assert body["name"] == "hello.txt"
    assert body["bucket"] == "mybucket"
    assert body["size"] == len(data)
    assert "download_token" in body


def test_storage_download_bytes(server):
    port = server["port"]
    data = b"hello storage"
    b64 = base64.b64encode(data).decode()
    _req(port, "POST", "/v1/storage/mybucket/o/hello.txt", {
        "base64_data": b64,
        "content_type": "text/plain",
    })
    status, raw = _req_bytes(port, "GET", "/v1/storage/mybucket/o/hello.txt")
    assert status == 200
    assert raw == data


def test_storage_download_missing_404(server):
    port = server["port"]
    status, _ = _req(port, "GET", "/v1/storage/mybucket/o/ghost.txt")
    assert status == 404


def test_storage_list_objects(server):
    port = server["port"]
    _req(port, "POST", "/v1/storage/listbucket/o/a.txt",
         {"base64_data": base64.b64encode(b"a").decode(), "content_type": "text/plain"})
    _req(port, "POST", "/v1/storage/listbucket/o/b.txt",
         {"base64_data": base64.b64encode(b"b").decode(), "content_type": "text/plain"})
    status, body = _req(port, "GET", "/v1/storage/listbucket/o")
    assert status == 200
    names = {o["name"] for o in body["objects"]}
    assert {"a.txt", "b.txt"} <= names


def test_storage_list_objects_with_prefix(server):
    port = server["port"]
    _req(port, "POST", "/v1/storage/pfxbucket/o/images/cat.png",
         {"base64_data": base64.b64encode(b"cat").decode(), "content_type": "image/png"})
    _req(port, "POST", "/v1/storage/pfxbucket/o/docs/readme.txt",
         {"base64_data": base64.b64encode(b"hi").decode(), "content_type": "text/plain"})
    status, body = _req(port, "GET", "/v1/storage/pfxbucket/o?prefix=images/")
    assert status == 200
    assert all(o["name"].startswith("images/") for o in body["objects"])


def test_storage_get_metadata(server):
    port = server["port"]
    _req(port, "POST", "/v1/storage/metabucket/o/f.txt",
         {"base64_data": base64.b64encode(b"data").decode(),
          "content_type": "text/plain",
          "custom_metadata": {"author": "test"}})
    status, body = _req(port, "GET", "/v1/storage/metabucket/o/f.txt/meta")
    assert status == 200
    assert body["name"] == "f.txt"
    assert body["custom_metadata"]["author"] == "test"


def test_storage_patch_metadata(server):
    port = server["port"]
    _req(port, "POST", "/v1/storage/metabucket/o/g.txt",
         {"base64_data": base64.b64encode(b"x").decode(), "content_type": "text/plain"})
    status, body = _req(port, "PATCH", "/v1/storage/metabucket/o/g.txt/meta",
                        {"custom_metadata": {"tag": "new"}})
    assert status == 200
    assert body["custom_metadata"]["tag"] == "new"


def test_storage_patch_metadata_missing_404(server):
    port = server["port"]
    status, _ = _req(port, "PATCH", "/v1/storage/metabucket/o/ghost.txt/meta",
                     {"custom_metadata": {}})
    assert status == 404


def test_storage_delete_object(server):
    port = server["port"]
    _req(port, "POST", "/v1/storage/delbucket/o/todelete.txt",
         {"base64_data": base64.b64encode(b"bye").decode(), "content_type": "text/plain"})
    status, body = _req(port, "DELETE", "/v1/storage/delbucket/o/todelete.txt")
    assert status == 200 and body["deleted"] is True
    status, _ = _req(port, "GET", "/v1/storage/delbucket/o/todelete.txt")
    assert status == 404


def test_storage_rotate_token(server):
    port = server["port"]
    _req(port, "POST", "/v1/storage/tokenbucket/o/f.txt",
         {"base64_data": base64.b64encode(b"x").decode(), "content_type": "text/plain"})
    _, meta1 = _req(port, "GET", "/v1/storage/tokenbucket/o/f.txt/meta")
    old_token = meta1["download_token"]
    status, body = _req(port, "POST", "/v1/storage/tokenbucket/o/f.txt/token")
    assert status == 200
    assert body["token"] != old_token
    _, meta2 = _req(port, "GET", "/v1/storage/tokenbucket/o/f.txt/meta")
    assert meta2["download_token"] == body["token"]


def test_storage_rotate_token_missing_404(server):
    port = server["port"]
    status, _ = _req(port, "POST", "/v1/storage/tokenbucket/o/ghost.txt/token")
    assert status == 404


def test_storage_binary_upload_via_raw_bytes(server):
    port = server["port"]
    data = bytes(range(256))
    status, body = _req(port, "POST", "/v1/storage/binbucket/o/binary.bin",
                        raw_bytes=data, content_type="application/octet-stream")
    assert status == 201
    assert body["size"] == 256
    dl_status, dl_bytes = _req_bytes(port, "GET", "/v1/storage/binbucket/o/binary.bin")
    assert dl_status == 200
    assert dl_bytes == data
