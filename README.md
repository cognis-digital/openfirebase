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

It gives a developer eleven things behind one local HTTP server:

- a **document database** (collections of JSON documents with `where` queries,
  subcollections, transactions, batched writes, and FieldValue sentinels),
- a **realtime JSON tree** (read/write/merge/push at any path, orderByChild
  queries, atomic transactions, onDisconnect/presence stubs),
- a **local auth** service (email/password + custom-token + provider-sign-in
  stubs, PBKDF2 hashing, HMAC tokens with custom claims, user CRUD + listUsers,
  password-reset and email-verification OTP flows — for dev only, **not** a
  real identity provider),
- a **static hosting** server with rewrites, redirects, custom headers, and
  preview channels,
- a **function-trigger runner** that fires Python handlers on Firestore/RTDB
  database events, Auth events, Storage events, HTTP requests, callable
  functions (structured errors), Pub/Sub messages, and scheduled jobs,
- a **Cloud Storage** emulator (bucket/object store with upload/download/metadata
  and download tokens),
- a **Security Rules engine** (parse and evaluate a meaningful subset of the
  Firestore/Storage rules DSL — wildcards, `request.auth`, `resource.data`,
  type checks, `&&`/`||`/`!` — wired into the server via load/check endpoints),
- a **Remote Config** store (parameters with default + conditional values,
  named conditions, fetch-and-evaluate for a client context),
- a **Cloud Messaging (FCM)** emulator (device token registry, topic
  subscribe/unsubscribe, send to token/topic/multicast, local inbox for test
  assertions — no real delivery),
- an **App Check** emulator (issue/verify HMAC-signed tokens for all four
  attestation providers in local mode, token revocation by JTI), and
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
  rules.py        Security Rules engine — parse + evaluate Firestore/Storage DSL
  remoteconfig.py Remote Config — parameters, conditions, fetch/evaluate
  messaging.py    Cloud Messaging (FCM) — token registry, topics, send, inbox
  appcheck.py     App Check — issue/verify HMAC tokens, revocation, app registry
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
| Auth              | Authentication (deep)            | `auth.py`         | `/v1/auth/...`                    | email+password, PBKDF2 hashing, HMAC tokens, custom-token mint/verify, custom claims, `update_user`, `list_users` (paginated), password-reset OTP flow, email-verification OTP flow, provider sign-in stubs (Google/GitHub/Facebook/Twitter/Apple/Microsoft/anonymous), `set_custom_claims`, Auth triggers (onCreate/onDelete) |
| Hosting           | Hosting (deep)                   | `hosting.py`      | `/` (static) `/v1/hosting/...`    | directory index, SPA fallback, path-traversal guard, **rewrites** (path + function proxy), **redirects** (301/302/307/308), **custom headers** per glob pattern, **preview channels** (named overlay dirs) |
| Functions         | Cloud Functions (deep)           | `functions.py`    | `/v1/functions/...`               | DB triggers (Firestore/RTDB, onCreate/onWrite/onUpdate/onDelete), **callable functions** (`on_call`, `FunctionError`), **Auth triggers** (onAuthUserCreate/onAuthUserDelete), **Storage triggers** (onStorageObjectFinalize/onStorageObjectDelete), **Pub/Sub** (publish + subscribe, per-topic), **scheduled functions** (register + run on demand), `onRequest` HTTP handlers, error isolation |
| Cloud Storage     | Cloud Storage for Firebase       | `cloudstorage.py` | `/v1/storage/...`                 | buckets, objects (upload/download/delete/list), metadata (get/patch/custom_metadata), download tokens (generate/rotate), prefix listing, binary + base64 upload, MD5 checksum, Storage triggers wired to Functions |
| Security Rules    | Firebase Security Rules (subset) | `rules.py`        | `/v1/rules/...`                   | parse+evaluate a meaningful subset of Firestore/Storage rules DSL (`allow read/write/get/list/create/update/delete`), static+wildcard path matching, `{wildcard}` + `{wildcard=**}` double-wildcard, `request.auth`, `resource.data`, `request.resource.data`, `is` type checks, `&&`/`\|\|`/`!`, wired into server `load`+`check` endpoints |
| Remote Config     | Remote Config                    | `remoteconfig.py` | `/v1/remoteconfig/...`            | parameters (default + conditional values), named conditions (field/op/value predicates — `==`/`!=`/`contains`/`startsWith`/`matches`), `fetch` evaluates all params for a client context (first matching condition wins), `evaluate` single key, full template with version counter |
| Cloud Messaging   | Firebase Cloud Messaging (FCM)   | `messaging.py`    | `/v1/messaging/...`               | device token registry, topic subscribe/unsubscribe, `send_to_token` / `send_to_topic` / `send_multicast` capture messages in local inbox, inbox list/get/clear, all messages stored for test assertions — no real delivery |
| App Check         | Firebase App Check               | `appcheck.py`     | `/v1/appcheck/...`                | app registration, HMAC-signed tokens (`issue_token` — debug/device_check/play_integrity/app_attest providers all accepted in local mode), `verify_token` (signature + expiry + revocation + optional app_id match), token revocation by JTI, token listing |

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

### Auth (deep — messaging+compute pass)

```
POST   /v1/auth/signup                           create user (email+password)
POST   /v1/auth/signin                           sign in (email+password)
POST   /v1/auth/verify                           verify id-token
POST   /v1/auth/custom-token                     mint custom token
    {"uid":"...", "custom_claims":{...}, "ttl": 3600}
POST   /v1/auth/verify-custom-token              verify custom token
    {"token":"..."}
GET    /v1/auth/users                            list users (paginated)
GET    /v1/auth/users/<uid>                      get user by uid
PATCH  /v1/auth/users/<uid>                      update user (display_name/email/password/disabled/email_verified/custom_claims)
DELETE /v1/auth/users/<uid>                      delete user (fires onAuthUserDelete)
POST   /v1/auth/password-reset                   password reset flow
    {"action":"generate","email":"..."}          → {"reset_token":"..."}
    {"action":"confirm","reset_token":"...","new_password":"..."}
POST   /v1/auth/email-verification               email verification flow
    {"action":"generate","uid":"..."}            → {"verification_token":"..."}
    {"action":"confirm","verification_token":"..."}
POST   /v1/auth/provider-signin                  provider sign-in stub
    {"provider_id":"google.com","provider_uid":"...","email":"...","display_name":"..."}
POST   /v1/auth/set-custom-claims                set custom claims
    {"uid":"...", "custom_claims":{...}}
```

### Functions (deep — messaging+compute pass)

```
GET    /v1/functions                             list all handlers (all types)
POST   /v1/functions/<name>                      invoke onRequest handler
POST   /v1/functions/_callable/<name>            invoke callable function
    {"data":{...}, "context":{...}}              → {"result":...} or {"error":{...}}
POST   /v1/functions/_pubsub/<topic>             publish Pub/Sub message
    {"message":{...}}                            → {"topic":..., "count":..., "results":[...]}
POST   /v1/functions/_schedule/<name>            run scheduled function on demand
                                                 → {"result":...}
```

### Hosting management (messaging+compute pass)

```
GET    /v1/hosting/channels                      list preview channels
POST   /v1/hosting/channels/<name>               create preview channel
    {"dir":"/path/to/overlay"}                   → {"name":"...", "url":"..."}
DELETE /v1/hosting/channels/<name>               delete preview channel
```

Hosting rewrites, redirects, and headers are configured in-process via the
`Hosting` constructor kwargs (`rewrites`, `redirects`, `headers`) or by
mutating the `app.hosting.rewrites` / `app.hosting.redirects` /
`app.hosting.headers_rules` lists.

### Security Rules (identity+security pass)

```
POST   /v1/rules/load                            load rules DSL
    {"rules": "<rules source string>"}           → {"status":"ok"}
POST   /v1/rules/check                           evaluate a rule
    {
        "service":              "cloud.firestore" | "firebase.storage",
        "path":                 "/collection/doc",
        "operation":            "get"|"list"|"create"|"update"|"delete",
        "auth":                 {"sub":"uid",...} | null,
        "resource_data":        {...},
        "request_resource_data":{...}
    }                                            → {"allowed": true|false}
```

### Remote Config (identity+security pass)

```
GET    /v1/remoteconfig/template                 full template (conditions+parameters+version)
POST   /v1/remoteconfig/fetch                    evaluate config for client context
    {"client_context": {"platform":"android",...}} → {"config":{key:value,...},"version":N}
GET    /v1/remoteconfig/parameters               list all parameters
POST   /v1/remoteconfig/parameters               create/replace parameter
    {"key":"...","default_value":"...","conditional_values":[{"condition":"...","value":"..."},...]}
GET    /v1/remoteconfig/parameters/<key>         get parameter
PUT    /v1/remoteconfig/parameters/<key>         replace parameter
DELETE /v1/remoteconfig/parameters/<key>         delete parameter
GET    /v1/remoteconfig/conditions               list conditions
POST   /v1/remoteconfig/conditions               create condition
    {"name":"...","expression":[{"field":"...","op":"==","value":"..."},...]}
GET    /v1/remoteconfig/conditions/<name>        get condition
DELETE /v1/remoteconfig/conditions/<name>        delete condition
```

Condition ops: `==`, `!=`, `contains`, `startsWith`, `matches` (regex).

### Cloud Messaging / FCM (identity+security pass)

```
POST   /v1/messaging/tokens                      register device token
    {"token":"...","metadata":{...}}             → token record
GET    /v1/messaging/tokens                      list registered tokens
GET    /v1/messaging/tokens/<token>              get token record
DELETE /v1/messaging/tokens/<token>              unregister token

GET    /v1/messaging/topics                      list topics + subscriber lists
GET    /v1/messaging/topics/<topic>              get topic
POST   /v1/messaging/topics/<topic>/subscribe    subscribe token
    {"token":"..."}
POST   /v1/messaging/topics/<topic>/unsubscribe  unsubscribe token
    {"token":"..."}

POST   /v1/messaging/send                        capture a message
    {"target_type":"token","token":"...","notification":{...},"data":{...}}
    {"target_type":"topic","topic":"...","notification":{...},"data":{...}}
    {"target_type":"multicast","tokens":[...],"notification":{...},"data":{...}}

GET    /v1/messaging/messages                    list captured messages (inbox)
GET    /v1/messaging/messages/<message_id>       get message
```

### App Check (identity+security pass)

```
POST   /v1/appcheck/apps                         register app
    {"app_id":"...","providers":[...]}           → app record
GET    /v1/appcheck/apps                         list registered apps
GET    /v1/appcheck/apps/<app_id>                get app
DELETE /v1/appcheck/apps/<app_id>                unregister app

POST   /v1/appcheck/tokens                       issue App Check token
    {"app_id":"...","provider":"debug","attestation_data":{...},"ttl":3600}
    → {"token":"<signed-token>"}
GET    /v1/appcheck/tokens                       list issued tokens (metadata)
POST   /v1/appcheck/tokens/verify                verify token
    {"token":"...","app_id":"..."}               → {"valid":true,"claims":{...}}
POST   /v1/appcheck/tokens/<jti>/revoke          revoke token by JTI
                                                 → {"revoked":true|false}
```

Supported providers: `debug`, `device_check`, `play_integrity`, `app_attest`
(all accepted in local mode without real attestation).

## Library API highlights (new in messaging+compute pass)

```python
from openfirebase import (
    AuthService, FunctionRegistry, FunctionError, Hosting,
    ON_AUTH_USER_CREATE, ON_AUTH_USER_DELETE,
    ON_STORAGE_FINALIZE, ON_STORAGE_DELETE,
)

# ---- Auth deep features ----
auth = AuthService(secret="dev-secret")
user = auth.sign_up("ada@example.com", "password1")

# Custom token with claims
token = auth.mint_custom_token(user["uid"], custom_claims={"role": "admin"})
payload = auth.verify_custom_token(token)  # payload["custom_claims"]["role"] == "admin"

# User CRUD
auth.update_user(user["uid"], display_name="Ada", custom_claims={"plan": "pro"})
auth.update_user(user["uid"], disabled=True)      # blocks sign-in
page = auth.list_users(page_size=50)              # {"users": [...], "next_page_token": ...}

# Password reset OTP flow
reset_tok = auth.generate_password_reset_token("ada@example.com")
auth.confirm_password_reset(reset_tok, "new-password1")

# Email verification OTP flow
verify_tok = auth.generate_email_verification_token(user["uid"])
auth.confirm_email_verification(verify_tok)       # sets email_verified=True

# Provider sign-in stub
result = auth.sign_in_with_provider("google.com", "google-uid-123",
                                    email="ada@gmail.com")
# result["id_token"] is a valid local token for the linked/created user

# ---- Functions deep features ----
reg = FunctionRegistry()

# Callable function (Firebase callable-style)
@reg.on_call("add")
def add(data, context):
    return data["a"] + data["b"]

result = reg.call_callable("add", {"a": 3, "b": 4})  # {"result": 7}

# Structured error
@reg.on_call("check")
def check(data, context):
    raise FunctionError("permission denied", code="permission-denied")

# Auth triggers
@reg.on_auth_user(ON_AUTH_USER_CREATE)
def on_user_created(user):
    print(f"new user: {user['uid']}")

# Storage triggers
@reg.on_storage(ON_STORAGE_FINALIZE, bucket_prefix="uploads/")
def on_upload(meta):
    print(f"uploaded: {meta['name']} ({meta['size']} bytes)")

# Pub/Sub
@reg.on_pubsub("events")
def on_event(ctx):
    print(f"received: {ctx['message']}")

reg.publish("events", {"type": "click"})  # fires all subscribers

# Scheduled
@reg.schedule("nightly", cron="0 0 * * *")
def nightly(ctx):
    return "done"

reg.run_scheduled("nightly")

# ---- Hosting deep features ----
hosting = Hosting(
    "./public",
    spa_fallback=True,
    rewrites=[
        {"source": "/api/**", "function": "myApiFunction"},
    ],
    redirects=[
        {"source": "/old", "destination": "/new", "type": 301},
    ],
    headers=[
        {"source": "**/*.html", "headers": [
            {"key": "X-Frame-Options", "value": "DENY"},
        ]},
    ],
)

# Preview channels
hosting.create_channel("beta", "./public-beta")
hosting.get_channel_url("beta")   # http://localhost:8080/__channel/beta/
# serve with channel overlay:
hosting.serve("/index.html", channel="beta")
hosting.serve_with_headers("/index.html")  # (data, ctype, extra_headers)
```

## Library API highlights (identity+security pass)

```python
from openfirebase import (
    RulesEngine, PermissionDenied,
    RemoteConfig,
    CloudMessaging,
    AppCheck, AppCheckError,
)

# ---- Security Rules ----
engine = RulesEngine()
engine.load_rules('''
    service cloud.firestore {
        match /users/{uid} {
            allow read: if true;
            allow write: if request.auth != null && request.auth.uid == uid;
        }
    }
''')
ctx = engine.make_context(auth_payload={"sub": "u123"})
engine.check("cloud.firestore", "/users/u123", "create", ctx)  # OK
assert engine.is_allowed("cloud.firestore", "/users/u123", "create",
                         engine.make_context()) is False  # no auth

# ---- Remote Config ----
rc = RemoteConfig()
rc.set_condition("android", [{"field": "platform", "op": "==", "value": "android"}])
rc.set_parameter("theme", "light",
                 conditional_values=[{"condition": "android", "value": "dark"}])
config = rc.fetch({"platform": "android"})   # {"theme": "dark"}
val = rc.evaluate("theme", {"platform": "ios"})  # "light"

# ---- Cloud Messaging ----
msg = CloudMessaging()
msg.register_token("device_tok_abc", metadata={"platform": "android"})
msg.subscribe("device_tok_abc", "breaking-news")
msg.send_to_topic("breaking-news",
                  notification={"title": "Big story", "body": "Details..."})
msg.send_to_token("device_tok_abc", data={"action": "refresh"})
messages = msg.list_messages(target_type="token")
msg.clear_inbox()

# ---- App Check ----
ac = AppCheck(secret="dev-secret")
ac.register_app("1:123456:android:abcdef")
token = ac.issue_token("1:123456:android:abcdef", provider="debug")
payload = ac.verify_token(token)          # {"sub":"1:123456:android:abcdef",...}
ac.revoke_token(payload["jti"])
try:
    ac.verify_token(token)                # raises AppCheckError("token has been revoked")
except AppCheckError:
    pass
```

## Library API highlights (storage+data pass)

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

- **519 tests, all passing** (`python -m pytest -q`) on Python 3.14 locally.
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
upload, path-like object names), Auth (sign-up/sign-in/token verify/tamper/expiry/
wrong-secret, custom-token mint+verify, custom claims, update_user, list_users
pagination, password-reset OTP flow, email-verification OTP flow, provider
sign-in stubs for 7 providers incl. anonymous, disabled-user rejection, provider
index cleanup on delete), Functions (DB trigger dispatch + prefix filtering +
error isolation + HTTP invoke, callable functions + FunctionError, Auth triggers
onCreate/onDelete, Storage triggers onFinalize/onDelete + bucket prefix,
Pub/Sub publish+subscribe + multi-topic + error isolation, scheduled function
register+run + last_run tracking, full introspection for all handler types),
Hosting (serve/resolve/SPA-fallback/path-traversal + rewrites path+function,
redirects 301/302/default, custom headers merged-from-multiple-rules, preview
channels create/list/delete/overlay-shadow/fall-through, serve_with_headers
three-tuple API, backward-compat two-tuple API), storage triggers wired via
server (finalize on upload, delete on object delete), auth triggers wired via
server (onCreate on signup, onDelete on user delete), hosting management HTTP
endpoints (channels list/create/delete), callable+pubsub+schedule HTTP endpoints,
the CLI, and the full HTTP server end-to-end for all services.
Security Rules (tokeniser, path matching with single/double wildcards, all
expression operators — `==`/`!=`/`&&`/`||`/`!`/`is`, `request.auth` + uid +
token claims, `resource.data` + `request.resource.data` comparisons, compound
`read`/`write` op expansion, service routing, `is_allowed` bool helper, server
load/check endpoints), Remote Config (parameter CRUD, condition CRUD, fetch with
condition evaluation for all operators, `evaluate` single key, version counter,
full template, server endpoints), Cloud Messaging (token register/unregister,
topic subscribe/unsubscribe/idempotency/cascading-unregister, send to token /
topic / multicast, inbox list/get/filter/limit/clear, data-as-strings coercion,
server round-trip for all operations), App Check (app register/get/list/unregister,
token issue for all four providers, custom TTL, attestation data embedding, verify
success + app_id match + mismatch + tamper + wrong secret + expired + wrong-type +
revoked, revocation by JTI, list-filtered-by-app_id, server round-trip for all
operations).

## Roadmap (not yet implemented)

These are intentionally **not** built yet and are listed honestly so nothing is
overclaimed:

- Firestore: collection-group queries, collection enumeration, real-time
  listeners/streaming, index-enforced composite sorts.
- Realtime DB: server-sent-event subscriptions, persistent
  onDisconnect across connections (current stub is in-process only).
- Auth: real OAuth/OIDC round-trips (current provider sign-in is a stub that
  skips the provider redirect flow); refresh tokens (current tokens are
  short-lived HMAC blobs for local dev only).
- Functions: async/background execution, true cron scheduling (current scheduled
  functions run only when triggered via the HTTP endpoint or `run_scheduled()`
  in-process; no built-in wall-clock scheduler).
- Cloud Storage: ACL/IAM rules, resumable uploads, object versioning, signed URLs
  (current download tokens are opaque UUIDs, not time-limited signed URLs).
- Hosting: CDN-style cache headers, i18n rewrites, multi-site support beyond
  named in-process overlay channels.
- Security Rules: full `get()` / `exists()` cross-document reads inside rule
  expressions, `request.time` / duration checks, full Firestore path function
  set (`path()`, `toSet()`, etc.), server-side rule enforcement wired into
  every Firestore/Storage operation (current wiring is via explicit `check`
  endpoint only).
- Remote Config: A/B test percentile targeting, rollout percentage conditions,
  user-property targeting beyond custom field comparisons.
- Cloud Messaging: actual delivery to real FCM endpoints, collapse-key / TTL
  semantics, analytics event tracking.
- App Check: real Play Integrity / DeviceCheck / App Attest network verification
  (current providers are all accepted locally without real attestation); token
  exchange endpoint compatible with the Firebase SDK.

## Interoperability

`openfirebase` composes with the 300+ tool Cognis suite — JSON in/out and a shared
OpenAI-compatible `/v1` backbone. See **[INTEROP.md](INTEROP.md)** for the
suite map, composition patterns, and reference stacks.

## Integrations

Forward `openfirebase`'s findings to STIX/MISP/Sigma/Splunk/Elastic/Slack/webhooks via
[`cognis-connect`](https://github.com/cognis-digital/cognis-connect). See **[INTEGRATIONS.md](INTEGRATIONS.md)**.

## License

Released under the **Cognis Open Collaboration License (COCL) 1.0** — see
[`LICENSE`](LICENSE).
