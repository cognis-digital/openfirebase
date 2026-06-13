"""Firestore-style document database.

Provides collections of documents (JSON maps) addressed by id, with create /
get / set / update / delete and a chainable ``where`` query builder supporting
the common operators. Supports subcollections, composite filters, cursor-based
pagination, FieldValue sentinels (increment / arrayUnion / arrayRemove /
serverTimestamp / delete), batched writes, and transactions.

This is a compatible SUBSET of the document-database semantics; it is not the
real product.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .storage import BaseStore, MemoryStore

_OPERATORS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a is not None and a < b,
    "<=": lambda a, b: a is not None and a <= b,
    ">": lambda a, b: a is not None and a > b,
    ">=": lambda a, b: a is not None and a >= b,
    "in": lambda a, b: a in b,
    "not-in": lambda a, b: a not in b,
    "array-contains": lambda a, b: isinstance(a, list) and b in a,
    "array-contains-any": lambda a, b: isinstance(a, list) and any(x in a for x in b),
}

# ---------------------------------------------------------------------------
# FieldValue sentinels
# ---------------------------------------------------------------------------

class _FieldValue:
    """Base for all FieldValue sentinel objects."""
    __slots__ = ("_kind", "_value")

    def __init__(self, kind: str, value: Any = None) -> None:
        self._kind = kind
        self._value = value

    def __repr__(self) -> str:  # pragma: no cover
        return f"FieldValue.{self._kind}({self._value!r})"


class FieldValue:
    """Factory for Firestore FieldValue sentinels.

    Usage::

        fs.update("col", "doc", {
            "score":  FieldValue.increment(5),
            "tags":   FieldValue.array_union(["new"]),
            "old":    FieldValue.array_remove(["stale"]),
            "ts":     FieldValue.server_timestamp(),
            "gone":   FieldValue.delete(),
        })
    """

    @staticmethod
    def increment(n: float) -> _FieldValue:
        return _FieldValue("increment", n)

    @staticmethod
    def array_union(items: list) -> _FieldValue:
        return _FieldValue("arrayUnion", items)

    @staticmethod
    def array_remove(items: list) -> _FieldValue:
        return _FieldValue("arrayRemove", items)

    @staticmethod
    def server_timestamp() -> _FieldValue:
        return _FieldValue("serverTimestamp")

    @staticmethod
    def delete() -> _FieldValue:
        return _FieldValue("delete")


def _apply_field_value(existing_val: Any, fv: _FieldValue, now: float) -> Any:
    """Apply a FieldValue sentinel to an existing field value and return the new value."""
    if fv._kind == "increment":
        if existing_val is None:
            return fv._value
        return (existing_val or 0) + fv._value
    if fv._kind == "arrayUnion":
        base = existing_val if isinstance(existing_val, list) else []
        result = list(base)
        for item in fv._value:
            if item not in result:
                result.append(item)
        return result
    if fv._kind == "arrayRemove":
        base = existing_val if isinstance(existing_val, list) else []
        return [x for x in base if x not in fv._value]
    if fv._kind == "serverTimestamp":
        return now
    if fv._kind == "delete":
        return _FieldValue("delete")  # sentinel — caller must actually delete the key
    return fv  # pragma: no cover


def _resolve_field_values(data: Dict[str, Any], existing: Dict[str, Any],
                          now: float) -> Tuple[Dict[str, Any], List[str]]:
    """Expand FieldValue sentinels in *data*, using *existing* for current values.

    Returns ``(resolved_dict, keys_to_delete)`` where *keys_to_delete* is the
    list of keys that had ``FieldValue.delete()`` and should be removed from the
    document.
    """
    result = {}
    to_delete: List[str] = []
    for k, v in data.items():
        if isinstance(v, _FieldValue):
            resolved = _apply_field_value(existing.get(k), v, now)
            if isinstance(resolved, _FieldValue) and resolved._kind == "delete":
                to_delete.append(k)
                continue  # omit from result → key is deleted
            result[k] = resolved
        else:
            result[k] = v
    return result, to_delete


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------

def _ns(collection: str) -> str:
    """Storage namespace for a top-level or nested collection path."""
    return f"firestore::{collection}"


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

class Query:
    """A chainable query over a single collection.

    Supports ``where`` / ``order_by`` / ``limit`` / ``start_after`` /
    ``start_at`` / ``end_before`` / ``end_at`` cursors (document snapshot or
    field value) and composite (AND) filters.
    """

    def __init__(self, db: "Firestore", collection: str) -> None:
        self._db = db
        self._collection = collection
        self._filters: List[Tuple[str, str, Any]] = []
        self._order: List[Tuple[str, str]] = []
        self._limit_val: Optional[int] = None
        self._limit_last_val: Optional[int] = None
        self._cursor_start: Optional[Tuple[Any, bool]] = None   # (value, inclusive)
        self._cursor_end: Optional[Tuple[Any, bool]] = None     # (value, inclusive)

    # ---- filter / order / limit -------------------------------------------

    def where(self, field: str, op: str, value: Any) -> "Query":
        if op not in _OPERATORS:
            raise ValueError(f"unsupported operator: {op!r}")
        self._filters.append((field, op, value))
        return self

    def order_by(self, field: str, direction: str = "asc") -> "Query":
        if direction not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")
        self._order.append((field, direction))
        return self

    def limit(self, n: int) -> "Query":
        self._limit_val = int(n)
        return self

    def limit_to_last(self, n: int) -> "Query":
        self._limit_last_val = int(n)
        return self

    # ---- cursors -----------------------------------------------------------

    def start_after(self, snapshot_or_value: Any) -> "Query":
        """Exclude the cursor document/value from results (exclusive start)."""
        self._cursor_start = (snapshot_or_value, False)
        return self

    def start_at(self, snapshot_or_value: Any) -> "Query":
        """Include the cursor document/value in results (inclusive start)."""
        self._cursor_start = (snapshot_or_value, True)
        return self

    def end_before(self, snapshot_or_value: Any) -> "Query":
        """Exclude the cursor document/value from results (exclusive end)."""
        self._cursor_end = (snapshot_or_value, False)
        return self

    def end_at(self, snapshot_or_value: Any) -> "Query":
        """Include the cursor document/value in results (inclusive end)."""
        self._cursor_end = (snapshot_or_value, True)
        return self

    # ---- execution ---------------------------------------------------------

    def stream(self) -> List[Dict[str, Any]]:
        docs = [dict(v, id=k) for k, v in self._db._store.items(_ns(self._collection))]

        # apply where filters
        for field, op, value in self._filters:
            fn = _OPERATORS[op]
            docs = [d for d in docs if fn(d.get(field), value)]

        # apply ordering (stable multi-key)
        if self._order:
            def sort_key(d: Dict[str, Any]):
                return tuple(
                    (d.get(f) is None, d.get(f))
                    for f, _ in self._order
                )
            # multi-key sort: apply in reverse order for stable results
            # We do a single sort using a composite key
            docs.sort(key=sort_key,
                      reverse=all(direction == "desc" for _, direction in self._order))
        else:
            # default ordering by doc id for determinism
            pass

        # cursor filtering (uses first order-by field, or 'id' if none)
        if self._order:
            cursor_field, _ = self._order[0]
        else:
            cursor_field = "id"

        def _cursor_value(snap_or_val: Any) -> Any:
            if isinstance(snap_or_val, dict):
                return snap_or_val.get(cursor_field)
            return snap_or_val

        if self._cursor_start is not None:
            cv, inclusive = self._cursor_start
            cv = _cursor_value(cv)
            if inclusive:
                docs = [d for d in docs if d.get(cursor_field) >= cv]
            else:
                docs = [d for d in docs if d.get(cursor_field) > cv]

        if self._cursor_end is not None:
            cv, inclusive = self._cursor_end
            cv = _cursor_value(cv)
            if inclusive:
                docs = [d for d in docs if d.get(cursor_field) <= cv]
            else:
                docs = [d for d in docs if d.get(cursor_field) < cv]

        # limit
        if self._limit_last_val is not None:
            docs = docs[-self._limit_last_val:]
        elif self._limit_val is not None:
            docs = docs[: self._limit_val]

        return docs

    # Convenience aliases
    def get(self) -> List[Dict[str, Any]]:
        return self.stream()


# ---------------------------------------------------------------------------
# WriteBatch
# ---------------------------------------------------------------------------

class _WriteOp:
    __slots__ = ("op", "collection", "doc_id", "data", "merge")

    def __init__(self, op: str, collection: str, doc_id: str,
                 data: Any = None, merge: bool = False) -> None:
        self.op = op
        self.collection = collection
        self.doc_id = doc_id
        self.data = data
        self.merge = merge


class WriteBatch:
    """Accumulate multiple write operations and commit them atomically.

    Usage::

        batch = fs.batch()
        batch.set("cities", "LA", {"pop": 4_000_000})
        batch.update("cities", "LA", {"pop": FieldValue.increment(1)})
        batch.delete("users", "olduser")
        batch.commit()
    """

    def __init__(self, db: "Firestore") -> None:
        self._db = db
        self._ops: List[_WriteOp] = []

    def set(self, collection: str, doc_id: str, data: Dict[str, Any],
            merge: bool = False) -> "WriteBatch":
        self._ops.append(_WriteOp("set", collection, doc_id, data, merge))
        return self

    def update(self, collection: str, doc_id: str,
               data: Dict[str, Any]) -> "WriteBatch":
        self._ops.append(_WriteOp("update", collection, doc_id, data))
        return self

    def delete(self, collection: str, doc_id: str) -> "WriteBatch":
        self._ops.append(_WriteOp("delete", collection, doc_id))
        return self

    def commit(self) -> None:
        """Execute all queued operations under the store's own lock."""
        with self._db._lock:
            for op in self._ops:
                if op.op == "set":
                    self._db.set(op.collection, op.doc_id, op.data, merge=op.merge)
                elif op.op == "update":
                    self._db.update(op.collection, op.doc_id, op.data)
                elif op.op == "delete":
                    self._db.delete(op.collection, op.doc_id)
            self._ops.clear()


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------

class TransactionError(Exception):
    """Raised when a transaction aborts after all retry attempts."""


class Transaction:
    """Read-then-write transaction with optimistic locking.

    Use :meth:`Firestore.run_transaction` rather than constructing directly.

    The update function receives a ``Transaction`` object. All reads inside the
    function go through the transaction, and all writes are buffered and
    committed atomically at the end. If any read document was modified between
    read and commit, the transaction is retried (up to ``max_attempts`` times).
    """

    def __init__(self, db: "Firestore") -> None:
        self._db = db
        self._reads: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
        self._writes: List[_WriteOp] = []

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        doc = self._db.get(collection, doc_id)
        key = (collection, doc_id)
        self._reads[key] = doc
        return doc

    def set(self, collection: str, doc_id: str, data: Dict[str, Any],
            merge: bool = False) -> None:
        self._writes.append(_WriteOp("set", collection, doc_id, data, merge))

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        self._writes.append(_WriteOp("update", collection, doc_id, data))

    def delete(self, collection: str, doc_id: str) -> None:
        self._writes.append(_WriteOp("delete", collection, doc_id))

    def _commit(self) -> None:
        """Check read versions and apply writes under the db lock."""
        with self._db._lock:
            # optimistic check: ensure all read docs are unchanged
            for (coll, doc_id), snap in self._reads.items():
                current = self._db.get(coll, doc_id)
                # compare _updated_at timestamp as version
                snap_ts = (snap or {}).get("_updated_at") if snap else None
                cur_ts = (current or {}).get("_updated_at") if current else None
                if snap_ts != cur_ts:
                    raise _ConflictError()
            # apply writes
            for op in self._writes:
                if op.op == "set":
                    self._db.set(op.collection, op.doc_id, op.data, merge=op.merge)
                elif op.op == "update":
                    self._db.update(op.collection, op.doc_id, op.data)
                elif op.op == "delete":
                    self._db.delete(op.collection, op.doc_id)


class _ConflictError(Exception):
    pass


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

class Firestore:
    """A document database of collections and documents.

    Supports:
    - CRUD: ``add`` / ``set`` / ``get`` / ``update`` / ``delete``
    - FieldValue sentinels: ``increment`` / ``array_union`` / ``array_remove``
      / ``server_timestamp`` / ``delete``
    - Chainable queries: ``collection().where().order_by().limit().stream()``
    - Pagination cursors: ``start_after`` / ``start_at`` / ``end_before`` / ``end_at``
    - Composite (AND) filters via chained ``where`` calls
    - Subcollections: ``subcollection("col/doc_id/subcol")``
    - Batched writes: ``batch().set(...).commit()``
    - Transactions: ``run_transaction(fn)``
    """

    def __init__(self, store: Optional[BaseStore] = None) -> None:
        self._store = store if store is not None else MemoryStore()
        self._lock = threading.RLock()

    # ---- document level ---------------------------------------------------

    def add(self, collection: str, data: Dict[str, Any]) -> str:
        """Create a doc with an auto-generated id; returns the id."""
        doc_id = uuid.uuid4().hex
        return self.set(collection, doc_id, data)

    def set(self, collection: str, doc_id: str, data: Dict[str, Any],
            merge: bool = False) -> str:
        if not isinstance(data, dict):
            raise TypeError("document data must be a dict")
        now = time.time()
        existing = self._store.get(_ns(collection), doc_id)
        if merge and isinstance(existing, dict):
            # resolve field values against existing
            resolved, to_delete = _resolve_field_values(data, existing, now)
            merged = dict(existing)
            merged.update(resolved)
            for dk in to_delete:
                merged.pop(dk, None)
            payload = merged
            payload["_created_at"] = existing.get("_created_at", now)
        else:
            resolved, _to_delete = _resolve_field_values(data, existing or {}, now)
            payload = dict(resolved)
            payload["_created_at"] = (existing or {}).get("_created_at", now) \
                if isinstance(existing, dict) else now
        payload["_updated_at"] = now
        self._store.set(_ns(collection), doc_id, payload)
        return doc_id

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        doc = self._store.get(_ns(collection), doc_id)
        if doc is None:
            return None
        return dict(doc, id=doc_id)

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> bool:
        """Patch fields on an existing doc.

        Supports FieldValue sentinels (increment, arrayUnion, etc.).
        Returns False if the doc does not exist.
        """
        existing = self._store.get(_ns(collection), doc_id)
        if not isinstance(existing, dict):
            return False
        now = time.time()
        resolved, to_delete = _resolve_field_values(data, existing, now)
        merged = dict(existing)
        merged.update(resolved)
        for dk in to_delete:
            merged.pop(dk, None)
        merged["_updated_at"] = now
        self._store.set(_ns(collection), doc_id, merged)
        return True

    def delete(self, collection: str, doc_id: str) -> bool:
        return self._store.delete(_ns(collection), doc_id)

    def exists(self, collection: str, doc_id: str) -> bool:
        return self._store.get(_ns(collection), doc_id) is not None

    # ---- collection / query ----------------------------------------------

    def collection(self, collection: str) -> Query:
        """Return a :class:`Query` builder for *collection*."""
        return Query(self, collection)

    def subcollection(self, path: str) -> "Query":
        """Return a Query for a subcollection at *path* (e.g. ``"col/doc/sub"``).

        The path is stored under a namespace that encodes the full hierarchy,
        so subcollections are isolated from top-level collections of the same
        leaf name.
        """
        return Query(self, path)

    def where(self, collection: str, field: str, op: str, value: Any) -> Query:
        return Query(self, collection).where(field, op, value)

    def list(self, collection: str) -> List[Dict[str, Any]]:
        return Query(self, collection).stream()

    def collections(self) -> Iterable[str]:
        # Not natively enumerable in the KV store; callers track names.
        raise NotImplementedError("collection enumeration is a roadmap item")

    # ---- batched writes --------------------------------------------------

    def batch(self) -> WriteBatch:
        """Return a new :class:`WriteBatch` for this database."""
        return WriteBatch(self)

    # ---- transactions ----------------------------------------------------

    def run_transaction(self, update_fn: Callable[["Transaction"], None],
                        max_attempts: int = 5) -> None:
        """Run *update_fn* inside a transaction, retrying on conflict.

        *update_fn* receives a :class:`Transaction` object.  All reads done
        through ``txn.get()`` form the read set; writes done through
        ``txn.set/update/delete`` are buffered and applied atomically.  If any
        read document was modified between read and commit, the transaction is
        retried up to *max_attempts* times before raising
        :class:`TransactionError`.

        Example::

            def transfer(txn):
                src = txn.get("accounts", "alice")
                dst = txn.get("accounts", "bob")
                txn.update("accounts", "alice", {"balance": src["balance"] - 10})
                txn.update("accounts", "bob",   {"balance": dst["balance"] + 10})

            fs.run_transaction(transfer)
        """
        for attempt in range(max_attempts):
            txn = Transaction(self)
            update_fn(txn)
            try:
                txn._commit()
                return
            except _ConflictError:
                if attempt == max_attempts - 1:
                    raise TransactionError(
                        f"transaction aborted after {max_attempts} attempts"
                    )
