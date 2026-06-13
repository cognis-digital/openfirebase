# openfirebase

## Usage — step by step

A typical local-backend lifecycle with the `openfirebase` console command:

1. **Install** the CLI (puts `openfirebase` on your PATH):

   ```bash
   pipx install git+https://github.com/cognis-digital/openfirebase.git
   ```

2. **Start the server.** Begin with an ephemeral in-memory instance on `127.0.0.1:8080`:

   ```bash
   openfirebase serve --memory
   ```

   For a persistent instance that also hosts a front-end build, point it at a data dir and a public dir (the `serve` subcommand also accepts `--host`, `--port`, `--secret`, and `--spa`):

   ```bash
   openfirebase serve --data-dir ./.openfirebase --public ./public --spa
   ```

3. **Write and read data** without curl using the realtime-tree convenience subcommands. `set` takes a path and a JSON value; `get` takes a path. Both honor the top-level `--data-dir` / `--memory` flags, so target the same store your server uses:

   ```bash
   openfirebase --data-dir ./.openfirebase set rooms/r1 '{"name":"Lobby"}'
   openfirebase --data-dir ./.openfirebase get rooms/r1
   ```

4. **Read the output.** `get` and `set` print the value as JSON to stdout, so pipe it straight into `jq` or a test assertion:

   ```bash
   openfirebase --data-dir ./.openfirebase get rooms/r1 | jq .name   # -> "Lobby"
   ```

5. **Use it in CI.** Seed fixtures against a throwaway data dir and assert on the JSON, no network or cloud account required:

   ```bash
   openfirebase version
   openfirebase --data-dir ./ci-fixture set users/u1 '{"name":"Ada"}'
   test "$(openfirebase --data-dir ./ci-fixture get users/u1 | jq -r .name)" = "Ada"
   ```

## What is this?

**openfirebase** is an independent, open-source **local** reimplementation of the
core developer primitives popularised by Firebase. You run it on your own machine
to build, test, and demo apps **offline** — no cloud project, no billing account,
no network round-trips. It is in the same spirit as LocalStack (for AWS), MinIO
(for S3), or the Firebase Emulator Suite: a small, fast, self-contained stand-in
for the real service that you point your app at during development.

It gives a developer seven things behind one local HTTP server:

- a **document database** (collections of JSON documents with `where` queries,
  subcollections, transactions, batched writes, and FieldValue sentinels),
- a **realtime JSON tree** (read/write/merge/push at any path, orderByChild
  queries, atomic transactions, onDisconnect/presence stubs),
- a **local auth** service (email/password sign-up & sign-in issuing signed
  local tokens you can verify — for dev only, **not** a real identity provider),
- a **static hosting** server for your front-end build output,
- a **function-trigger runner** that fires your Python handlers on database
  events (`onCreate` / `onWrite` / `onDelete`) or HTTP requests (`onRequest`),
- a **Cloud Storage** emulator (bucket/object store with upload/download/metadata
  and download tokens), and
- a single shared storage backend (in-memory or SQLite) wiring all of them
  together.

**Who it is for:** developers who want a zero-dependency, scriptable local
backend for prototypes, integration tests, CI, demos, and working on a plane.
It is pure Python standard library at its core, so it starts instantly and runs
anywhere Python runs.

> ### Disclaimer
> openfirebase is an **independent, open reimplementation** intended for **LOCAL
> development and testing only**. It is **NOT affiliated with, endorsed by, or
> sponsored by** Google or the Firebase product or any related vendor. Vendor and
> product names are used **only nominatively** to describe API compatibility and
> the developer concepts being emulated. openfirebase implements a **compatible
> SUBSET** of those concepts and is **not intended for production use** or to
> secure real systems or data.

## Architecture

```
openfirebase/
  __init__.py     public API surface
  __main__.py     enables `python -m openfirebase`
  storage.py      key/value backends: MemoryStore + SqliteStore
  firestore.py    document database + chainable Query + FieldValue + WriteBatch + Transaction
  rtdb.py         realtime JSON tree + RTDBQuery + transactions + OnDisconnect stub
  auth.py         local email/password auth + HMAC-signed local tokens
  hosting.py      static file server with index + SPA fallback + traversal guard
  functions.py    trigger registry + dispatcher (onCreate/onWrite/onRequest...)
  cloudstorage.py Cloud Storage emulator (buckets, objects, metadata, tokens)
  server.py       single ThreadingHTTPServer exposing every service
  cli.py          `openfirebase` console entry point + subcommands
tests/            end-to-end + unit pytest suite
```

All services share one storage backend. Pass a `--data-dir` to persist to a
single SQLite file, or use `--memory` (and the in-memory store in tests) for an
ephemeral instance. The HTTP layer is std-lib `http.server` only.

## Services

| Service           | Emulates                         | Module            | HTTP prefix                       | Highlights                                                                                                             |
|-------------------|----------------------------------|-------------------|-----------------------------------|------------------------------------------------------------------------------------------------------------------------|
| Firestore         | Cloud Firestore (subset)         | `firestore.py`    | `/v1/firestore/...`               | collections/documents, `where` (all operators incl. `array-contains-any`), `order_by`, `limit`/`limit_to_last`, cursor pagination (`start_after`/`start_at`/`end_before`/`end_at`), composite AND filters, subcollections, `FieldValue` (increment/arrayUnion/arrayRemove/serverTimestamp/delete), `WriteBatch`, `Transaction` (optimistic-lock + retry), merge |
| Realtime DB       | Realtime Database (subset)       | `rtdb.py`         | `/v1/rtdb/...`                    | JSON tree, `get`/`set`/`update`/`push`/`delete`, `RTDBQuery` (orderByChild/orderByKey/orderByValue/equalTo/startAt/endAt/limitToFirst/limitToLast), atomic `transaction`, `OnDisconnect` presence stub |
| Auth              | Authentication (subset)          | `auth.py`         | `/v1/auth/...`                    | email+password, PBKDF2 hashing, HMAC local tokens, verify                                                             |
| Hosting           | Hosting (subset)                 | `hosting.py`      | `/` (static)                      | directory index, SPA fallback, path-traversal protection                                                               |
| Functions         | Cloud Functions (subset)         | `functions.py`    | `/v1/functions/...`               | DB triggers + `onRequest` HTTP handlers, error isolation                                                               |
| Cloud Storage     | Cloud Storage for Firebase       | `cloudstorage.py` | `/v1/storage/...`                 | buckets, objects (upload/download/delete/list), metadata (get/patch/custom_metadata), download tokens (generate/rotate), prefix listing, binary + base64 upload, MD5 checksum |

## HTTP API reference

### Firestore

```
POST   /v1/firestore/<col>                      create doc (auto id)
GET    /v1/firestore/<col>/<id>                 get doc
PUT    /v1/firestore/<col>/<id>                 set/replace doc
PATCH  /v1/firestore/<col>/<id>                 update (merge) doc
DELETE /v1/firestore/<col>/<id>                 delete doc
GET    /v1/firestore/<col>                      list collection

# Subcollections — use ~ as separator in URL
POST   /v1/firestore/<col>/<doc>/~/<subcol>     add subcollection doc
GET    /v1/firestore/<col>/<doc>/~/<subcol>/<id>  get subcollection doc

# Advanced query
POST   /v1/firestore/_query/<col>               structured query (see below)
POST   /v1/firestore/_batch                     batched writes
POST   /v1/firestore/_transaction               server-side transaction
```

**POST /v1/firestore/_query/&lt;col&gt;** body:
```json
{
    "where":    [{"field": "price", "op": ">", "value": 2}],
    "order_by": [{"field": "price", "direction": "asc"}],
    "limit":    10,
    "start_after": {"price": 2},
    "end_at":      {"price": 5}
}
```

**POST /v1/firestore/_batch** body:
```json
{
    "writes": [
        {"op": "set",    "collection": "cities", "id": "LA", "data": {"pop": 4000000}},
        {"op": "update", "collection": "cities", "id": "LA", "data": {"pop": 4100000}},
        {"op": "delete", "collection": "old",    "id": "x"}
    ]
}
```

### Realtime Database

```
GET    /v1/rtdb/<path>                   read value
PUT    /v1/rtdb/<path>                   set value
PATCH  /v1/rtdb/<path>                   shallow merge
POST   /v1/rtdb/<path>                   push (auto-key child)
DELETE /v1/rtdb/<path>                   delete

# Query (all params are JSON-serialised query-string values)
GET    /v1/rtdb/_query/<path>?orderByChild=field&limitToFirst=5
GET    /v1/rtdb/_query/<path>?orderByKey=1&equalTo=mykey
GET    /v1/rtdb/_query/<path>?orderByValue=1&startAt=10&endAt=50

# Atomic transaction
POST   /v1/rtdb/_transaction/<path>      {"op":"increment","value":1}
                                         {"op":"set_if_null","value":true}
                                         {"op":"set","value":42}
```

### Cloud Storage

```
GET    /v1/storage                       list buckets
GET    /v1/storage/<bucket>/o            list objects (optional ?prefix=...)
POST   /v1/storage/<bucket>/o/<name>     upload object
    Content-Type: application/json → {"base64_data":"...","content_type":"...","custom_metadata":{}}
    Content-Type: <anything else>  → raw bytes
GET    /v1/storage/<bucket>/o/<name>     download object (returns raw bytes)
DELETE /v1/storage/<bucket>/o/<name>     delete object
GET    /v1/storage/<bucket>/o/<name>/meta    get object metadata
PATCH  /v1/storage/<bucket>/o/<name>/meta   update custom_metadata
POST   /v1/storage/<bucket>/o/<name>/token  rotate download token
```

## Library API highlights (new in storage+data pass)

```python
from openfirebase import Firestore, RealtimeDatabase, CloudStorage
from openfirebase.firestore import FieldValue, TransactionError

# ---- Firestore ----
fs = Firestore()
# FieldValue sentinels
fs.set("products", "p1", {"stock": 10, "tags": ["new"]})
fs.update("products", "p1", {
    "stock": FieldValue.increment(-1),
    "tags":  FieldValue.array_union(["sale"]),
    "draft": FieldValue.delete(),
    "ts":    FieldValue.server_timestamp(),
})

# Cursor pagination
page1 = fs.collection("products").order_by("price").limit(10).stream()
page2 = fs.collection("products").order_by("price").start_after(page1[-1]).limit(10).stream()

# Subcollection
fs.set("users/u1/orders", "o1", {"amount": 100})

# Batched writes
batch = fs.batch()
batch.set("c", "d1", {"v": 1}).update("c", "d2", {"v": 2}).delete("c", "d3")
batch.commit()

# Transactions
def transfer(txn):
    src = txn.get("accounts", "alice")
    dst = txn.get("accounts", "bob")
    txn.update("accounts", "alice", {"balance": src["balance"] - 10})
    txn.update("accounts", "bob",   {"balance": dst["balance"] + 10})

fs.run_transaction(transfer)

# ---- Realtime Database ----
db = RealtimeDatabase()
# Queries
results = db.query("/scores").order_by_child("score").limit_to_first(3).get()
equal = db.query("/msgs").order_by_child("uid").equal_to("u1").get()

# Atomic transaction
db.transaction("/counters/visits", lambda n: (n or 0) + 1)

# onDisconnect (presence stub)
db.on_disconnect("/presence/u1").set("offline")
db.simulate_disconnect("/presence/u1")

# ---- Cloud Storage ----
cs = CloudStorage()
bucket = cs.bucket("my-app.appspot.com")
bucket.upload("images/logo.png", open("logo.png", "rb").read(), "image/png")
data = bucket.download("images/logo.png")
meta = bucket.get_metadata("images/logo.png")
print(meta["download_token"])
token = bucket.rotate_token("images/logo.png")
objs = bucket.list_objects(prefix="images/")
```

## Quickstart

```bash
# start everything on http://127.0.0.1:8080 (ephemeral, in-memory)
openfirebase serve --memory

# ...or with persistence + static hosting
openfirebase serve --data-dir ./.openfirebase --public ./public
```

```bash
# Firestore: create + query
curl -s -XPOST localhost:8080/v1/firestore/products -d '{"name":"apple","price":3}'
curl -s -XPOST localhost:8080/v1/firestore/_query/products \
     -d '{"where":[{"field":"price","op":">","value":1}],"order_by":[{"field":"price","direction":"asc"}]}'

# Realtime DB: query
curl -s "localhost:8080/v1/rtdb/_query/scores?orderByChild=score&limitToFirst=5"

# RTDB transaction
curl -s -XPOST localhost:8080/v1/rtdb/_transaction/counters/visits -d '{"op":"increment","value":1}'

# Cloud Storage: upload (base64 envelope)
curl -s -XPOST localhost:8080/v1/storage/my-bucket/o/hello.txt \
     -H 'Content-Type: application/json' \
     -d '{"base64_data":"aGVsbG8=","content_type":"text/plain"}'
curl -s localhost:8080/v1/storage/my-bucket/o/hello.txt           # download bytes
curl -s localhost:8080/v1/storage/my-bucket/o/hello.txt/meta      # metadata
curl -s -XPOST localhost:8080/v1/storage/my-bucket/o/hello.txt/token  # rotate token
```

<!-- cognis:domains:start -->
## Domains

**Primary domain:** Cloud & DevTools  ·  **JTF MERIDIAN division:** ATHENA-PRIME · COGNI-2

**Topics:** `cognis` `devtools` `cloud` `developer-tools` `python` `cloud-emulator`

Part of the **Cognis Neural Suite** — 300+ source-available tools organized across 12 domains under the JTF MERIDIAN command structure. See the [suite on GitHub](https://github.com/cognis-digital) and [jtf-meridian](https://github.com/cognis-digital/jtf-meridian) for how the pieces fit together.
<!-- cognis:domains:end -->

## Install

openfirebase is **source-available** (it is not published to PyPI). Install it
directly from this Git repository.

**One-line installers** (auto-detect pipx / uv / pip):

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/cognis-digital/openfirebase/main/install.sh | bash
```

```powershell
# Windows (PowerShell)
iwr -useb https://raw.githubusercontent.com/cognis-digital/openfirebase/main/install.ps1 | iex
```

**Manual options:**

```bash
# pipx (isolated CLI; recommended)
pipx install git+https://github.com/cognis-digital/openfirebase.git

# uv
uv tool install git+https://github.com/cognis-digital/openfirebase.git

# pip
pip install git+https://github.com/cognis-digital/openfirebase.git
```

**From source (for development):**

```bash
git clone https://github.com/cognis-digital/openfirebase.git
cd openfirebase
pip install -e ".[test]"
python -m pytest -q
```

Requires **Python 3.10+**. The runtime core has **no third-party dependencies**
(standard library only); `pytest` is needed only to run the test suite.

## Topics / Domains

`firebase` · `firebase-emulator` · `local-development` · `offline-first` ·
`firestore` · `realtime-database` · `authentication` · `static-hosting` ·
`serverless-functions` · `cloud-storage` · `testing` · `developer-tools` ·
`python` · `stdlib`

## Verification

The test suite is real and end-to-end: the HTTP server is started in a
background thread and data is round-tripped through every service over the wire,
alongside direct unit tests of each service class and both storage backends.

- **204 tests, all passing** (`python -m pytest -q`) on Python 3.14 locally.
- CI runs the same suite on **ubuntu / macOS / windows × Python 3.10–3.13**
  (see `.github/workflows/ci.yml`).

Coverage by area: storage backends (memory + sqlite, incl. persistence),
Firestore CRUD + every query operator (incl. `array-contains-any`, `not-in`,
`!=`) + ordering/limit/limit-to-last/merge + cursor pagination
(start_after/start_at/end_before/end_at) + FieldValue (increment/arrayUnion/
arrayRemove/serverTimestamp/delete) + subcollections + WriteBatch + Transaction
(optimistic-lock/conflict-retry/abort), realtime tree nesting/merge/push-
ordering/delete + RTDBQuery (orderByChild/orderByKey/orderByValue/equalTo/
limitToFirst/limitToLast/startAt/endAt) + atomic transaction + OnDisconnect
(set/remove/update/cancel/simulate_disconnect), Cloud Storage (bucket lifecycle,
upload/download binary fidelity MD5 check, metadata get/patch/custom_metadata,
prefix listing, download-token generate/rotate, base64-envelope and raw-bytes
upload, path-like object names), auth sign-up/sign-in/token verify/tamper/expiry/
wrong-secret, function trigger dispatch + prefix filtering + error isolation +
HTTP invoke, hosting index/SPA/traversal, the CLI, and the full HTTP server
end-to-end.

## Roadmap (not yet implemented)

These are intentionally **not** built yet and are listed honestly so nothing is
overclaimed:

- Firestore: collection-group queries, collection enumeration, real-time
  listeners/streaming, index-enforced composite sorts, security rules.
- Realtime DB: server-sent-event subscriptions, security rules, persistent
  onDisconnect across connections (current stub is in-process only).
- Auth: OAuth/OIDC providers, email verification flows, password reset, refresh
  tokens (current tokens are short-lived HMAC blobs for local dev only).
- Functions: scheduled/pub-sub triggers, async/background execution.
- Cloud Storage: ACL/IAM rules, resumable uploads, object versioning, signed URLs
  (current download tokens are opaque UUIDs, not time-limited signed URLs).

## License

Released under the **Cognis Open Collaboration License (COCL) 1.0** — see
[`LICENSE`](LICENSE).
