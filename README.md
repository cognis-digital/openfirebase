# openfirebase

## What is this?

**openfirebase** is an independent, open-source **local** reimplementation of the
core developer primitives popularised by Firebase. You run it on your own machine
to build, test, and demo apps **offline** — no cloud project, no billing account,
no network round-trips. It is in the same spirit as LocalStack (for AWS), MinIO
(for S3), or the Firebase Emulator Suite: a small, fast, self-contained stand-in
for the real service that you point your app at during development.

It gives a developer five things behind one local HTTP server:

- a **document database** (collections of JSON documents with `where` queries),
- a **realtime JSON tree** (read/write/merge/push at any path),
- a **local auth** service (email/password sign-up & sign-in issuing signed
  local tokens you can verify — for dev only, **not** a real identity provider),
- a **static hosting** server for your front-end build output, and
- a **function-trigger runner** that fires your Python handlers on database
  events (`onCreate` / `onWrite` / `onDelete`) or HTTP requests (`onRequest`).

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
  firestore.py    document database + chainable Query (where/order_by/limit)
  rtdb.py         realtime JSON tree (get/set/update/push/delete by path)
  auth.py         local email/password auth + HMAC-signed local tokens
  hosting.py      static file server with index + SPA fallback + traversal guard
  functions.py    trigger registry + dispatcher (onCreate/onWrite/onRequest...)
  server.py       single ThreadingHTTPServer exposing every service
  cli.py          `openfirebase` console entry point + subcommands
tests/            end-to-end + unit pytest suite
```

All services share one storage backend. Pass a `--data-dir` to persist to a
single SQLite file, or use `--memory` (and the in-memory store in tests) for an
ephemeral instance. The HTTP layer is std-lib `http.server` only.

## Services

| Service    | Emulates                  | Module          | HTTP prefix          | Highlights                                                    |
|------------|---------------------------|-----------------|----------------------|---------------------------------------------------------------|
| Firestore  | Cloud Firestore (subset)  | `firestore.py`  | `/v1/firestore/...`  | collections/documents, `where` (`==`,`<`,`in`,`array-contains`,…), `order_by`, `limit`, merge |
| Realtime   | Realtime Database (subset)| `rtdb.py`       | `/v1/rtdb/...`       | JSON tree, `get`/`set`/`update`/`push` (sortable push ids)/`delete` |
| Auth       | Authentication (subset)   | `auth.py`       | `/v1/auth/...`       | email+password, PBKDF2 hashing, HMAC local tokens, verify     |
| Hosting    | Hosting (subset)          | `hosting.py`    | `/` (static)         | directory index, SPA fallback, path-traversal protection      |
| Functions  | Cloud Functions (subset)  | `functions.py`  | `/v1/functions/...`  | DB triggers + `onRequest` HTTP handlers, error isolation      |

## Quickstart

```bash
# start everything on http://127.0.0.1:8080 (ephemeral, in-memory)
openfirebase serve --memory

# ...or with persistence + static hosting
openfirebase serve --data-dir ./.openfirebase --public ./public
```

```bash
# Firestore: create + read a document
curl -s -XPOST localhost:8080/v1/firestore/users -d '{"name":"Ada","age":36}'
# -> {"id":"<doc_id>"}
curl -s localhost:8080/v1/firestore/users/<doc_id>

# Realtime tree: write + read
curl -s -XPUT localhost:8080/v1/rtdb/rooms/r1 -d '{"value":{"name":"Lobby"}}'
curl -s localhost:8080/v1/rtdb/rooms/r1

# Auth: sign up (returns a local id_token), then verify it
curl -s -XPOST localhost:8080/v1/auth/signup -d '{"email":"a@b.com","password":"secret1"}'
curl -s -XPOST localhost:8080/v1/auth/verify -d '{"id_token":"<token>"}'
```

Use it as a library too:

```python
from openfirebase import Firestore, RealtimeDatabase, AuthService

fs = Firestore()                       # in-memory
fs.set("cities", "LA", {"pop": 4_000_000})
print(fs.collection("cities").where("pop", ">", 1_000_000).stream())

db = RealtimeDatabase()
db.update("/users/u1", {"name": "Bo"})
db.push("/users/u1/messages", {"text": "hi"})

auth = AuthService(secret="dev")
token = auth.sign_up("a@b.com", "secret1") and auth.sign_in("a@b.com", "secret1")["id_token"]
print(auth.verify_token(token))
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
`serverless-functions` · `testing` · `developer-tools` · `python` · `stdlib`

## Verification

The test suite is real and end-to-end: the HTTP server is started in a
background thread and data is round-tripped through every service over the wire,
alongside direct unit tests of each service class and both storage backends.

- **85 tests, all passing** (`python -m pytest -q`) on Python 3.14 locally.
- CI runs the same suite on **ubuntu / macOS / windows × Python 3.10–3.13**
  (see `.github/workflows/ci.yml`).

Coverage by area: storage backends (memory + sqlite, incl. persistence),
Firestore CRUD + every query operator + ordering/limit/merge, realtime tree
nesting/merge/push-ordering/delete, auth sign-up/sign-in/token verify/tamper/
expiry/wrong-secret, function trigger dispatch + prefix filtering + error
isolation + HTTP invoke, hosting index/SPA/traversal, the CLI, and the full
HTTP server end-to-end.

## Roadmap (not yet implemented)

These are intentionally **not** built yet and are listed honestly so nothing is
overclaimed:

- Firestore: composite indexes, sub-collections, transactions, real-time
  listeners/streaming, full pagination cursors, collection enumeration.
- Realtime DB: server-sent-event subscriptions, security rules, transactions.
- Auth: OAuth/OIDC providers, email verification flows, password reset, refresh
  tokens (current tokens are short-lived HMAC blobs for local dev only).
- Functions: scheduled/pub-sub triggers, async/background execution.
- Storage: a Cloud-Storage-style object/blob bucket service.

## License

Released under the **Cognis Open Collaboration License (COCL) 1.0** — see
[`LICENSE`](LICENSE).
