"""Realtime-Database-style JSON tree.

A single JSON document addressed by slash-separated paths, supporting
``get`` / ``set`` / ``update`` / ``push`` / ``delete`` at any path. Adds
query helpers (``order_by_child`` / ``equal_to`` / ``limit_to_first`` /
``limit_to_last``), read-modify-write ``transaction``, and a stub
``on_disconnect`` registry (presence/onDisconnect semantics for local dev).

This is a compatible SUBSET of the realtime-tree semantics.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .storage import BaseStore, MemoryStore

_NS = "rtdb"
_ROOT_KEY = "root"


def _split(path: str) -> List[str]:
    return [p for p in path.strip("/").split("/") if p != ""]


def _push_id() -> str:
    """Chronologically-sortable push id (timestamp ms + counter, hex)."""
    ms = int(time.time() * 1000)
    return f"-{ms:012x}{_counter():04x}"


_counter_lock = threading.Lock()
_counter_val = 0


def _counter() -> int:
    global _counter_val
    with _counter_lock:
        _counter_val = (_counter_val + 1) % 0x10000
        return _counter_val


# ---------------------------------------------------------------------------
# RTDB Query
# ---------------------------------------------------------------------------

class RTDBQuery:
    """Chainable query builder over a RTDB collection (dict of push-id children).

    Mirrors the Firebase ``orderByChild`` / ``equalTo`` / ``limitToFirst`` /
    ``limitToLast`` / ``startAt`` / ``endAt`` API over an in-memory dict.

    Usage::

        results = db.query("/messages").order_by_child("score").limit_to_first(5).get()
    """

    def __init__(self, db: "RealtimeDatabase", path: str) -> None:
        self._db = db
        self._path = path
        self._order_child: Optional[str] = None
        self._equal_to_val: Any = _UNSET
        self._start_at_val: Any = _UNSET
        self._end_at_val: Any = _UNSET
        self._limit_first: Optional[int] = None
        self._limit_last: Optional[int] = None

    def order_by_child(self, child: str) -> "RTDBQuery":
        self._order_child = child
        return self

    def order_by_key(self) -> "RTDBQuery":
        self._order_child = "$key"
        return self

    def order_by_value(self) -> "RTDBQuery":
        self._order_child = "$value"
        return self

    def equal_to(self, value: Any) -> "RTDBQuery":
        self._equal_to_val = value
        return self

    def start_at(self, value: Any) -> "RTDBQuery":
        self._start_at_val = value
        return self

    def end_at(self, value: Any) -> "RTDBQuery":
        self._end_at_val = value
        return self

    def limit_to_first(self, n: int) -> "RTDBQuery":
        self._limit_first = int(n)
        return self

    def limit_to_last(self, n: int) -> "RTDBQuery":
        self._limit_last = int(n)
        return self

    def get(self) -> Dict[str, Any]:
        """Execute the query; returns a dict of matching children (key → value)."""
        node = self._db.get(self._path)
        if not isinstance(node, dict):
            return {}

        def _sort_key(item: Tuple[str, Any]) -> Any:
            k, v = item
            if self._order_child is None or self._order_child == "$key":
                return (k,)
            if self._order_child == "$value":
                return (v is None, v)
            child_val = v.get(self._order_child) if isinstance(v, dict) else None
            return (child_val is None, child_val)

        items = sorted(node.items(), key=_sort_key)

        # apply equalTo / startAt / endAt filters
        filtered = []
        for k, v in items:
            if self._order_child and self._order_child not in ("$key", "$value"):
                cmp_val = v.get(self._order_child) if isinstance(v, dict) else None
            elif self._order_child == "$key":
                cmp_val = k
            elif self._order_child == "$value":
                cmp_val = v
            else:
                cmp_val = k

            if self._equal_to_val is not _UNSET and cmp_val != self._equal_to_val:
                continue
            if self._start_at_val is not _UNSET:
                try:
                    if cmp_val < self._start_at_val:
                        continue
                except TypeError:
                    pass
            if self._end_at_val is not _UNSET:
                try:
                    if cmp_val > self._end_at_val:
                        continue
                except TypeError:
                    pass
            filtered.append((k, v))

        if self._limit_first is not None:
            filtered = filtered[: self._limit_first]
        elif self._limit_last is not None:
            filtered = filtered[-self._limit_last:]

        return dict(filtered)


_UNSET = object()


# ---------------------------------------------------------------------------
# OnDisconnect stub
# ---------------------------------------------------------------------------

class OnDisconnect:
    """Stub for onDisconnect operations (presence / offline handling).

    In a real RTDB these fire when a client disconnects. In this local
    emulator we store the registered operations and let callers trigger them
    via :meth:`RealtimeDatabase.simulate_disconnect` (useful in tests). This
    documents the API shape without requiring a persistent socket connection.
    """

    def __init__(self, db: "RealtimeDatabase", path: str) -> None:
        self._db = db
        self._path = path
        self._ops: List[Tuple[str, Any]] = []

    def set(self, value: Any) -> "OnDisconnect":
        self._ops.append(("set", value))
        return self

    def remove(self) -> "OnDisconnect":
        self._ops.append(("remove", None))
        return self

    def update(self, values: dict) -> "OnDisconnect":
        self._ops.append(("update", values))
        return self

    def cancel(self) -> None:
        self._ops.clear()

    def trigger(self) -> None:
        """Execute all registered operations (simulate a disconnect event)."""
        for op, value in self._ops:
            if op == "set":
                self._db.set(self._path, value)
            elif op == "remove":
                self._db.delete(self._path)
            elif op == "update":
                self._db.update(self._path, value)
        self._ops.clear()


# ---------------------------------------------------------------------------
# RealtimeDatabase
# ---------------------------------------------------------------------------

class RealtimeDatabase:
    """A single mutable JSON tree persisted as one document.

    Now includes:
    - ``query(path)`` — chainable query builder (orderByChild / equalTo / limit)
    - ``transaction(path, update_fn)`` — read-modify-write with retry
    - ``on_disconnect(path)`` — presence/onDisconnect stub
    - ``simulate_disconnect(path)`` — trigger on-disconnect ops in tests
    """

    def __init__(self, store: Optional[BaseStore] = None) -> None:
        self._store = store if store is not None else MemoryStore()
        self._lock = threading.RLock()
        # registry of OnDisconnect handlers keyed by path
        self._disconnect_handlers: Dict[str, OnDisconnect] = {}

    def _load(self) -> Any:
        return self._store.get(_NS, _ROOT_KEY) or {}

    def _save(self, root: Any) -> None:
        self._store.set(_NS, _ROOT_KEY, root)

    def get(self, path: str = "/") -> Any:
        with self._lock:
            node: Any = self._load()
            for part in _split(path):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return None
            return node

    def set(self, path: str, value: Any) -> Any:
        with self._lock:
            parts = _split(path)
            if not parts:
                self._save(value)
                return value
            root = self._load()
            if not isinstance(root, dict):
                root = {}
            node = root
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = value
            self._save(root)
            return value

    def update(self, path: str, values: dict) -> Any:
        """Shallow-merge ``values`` into the map at ``path``."""
        if not isinstance(values, dict):
            raise TypeError("update requires a dict of child values")
        with self._lock:
            current = self.get(path)
            if not isinstance(current, dict):
                current = {}
            merged = dict(current)
            merged.update(values)
            return self.set(path, merged)

    def push(self, path: str, value: Any) -> str:
        """Append a child under a new push id at ``path``; returns the id."""
        with self._lock:
            key = _push_id()
            child_path = f"{path.rstrip('/')}/{key}"
            self.set(child_path, value)
            return key

    def delete(self, path: str) -> bool:
        with self._lock:
            parts = _split(path)
            if not parts:
                self._save({})
                return True
            root = self._load()
            node = root
            for part in parts[:-1]:
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return False
            if isinstance(node, dict) and parts[-1] in node:
                del node[parts[-1]]
                self._save(root)
                return True
            return False

    # ---- query ------------------------------------------------------------

    def query(self, path: str) -> RTDBQuery:
        """Return a :class:`RTDBQuery` builder for the collection at *path*."""
        return RTDBQuery(self, path)

    # ---- transaction -------------------------------------------------------

    def transaction(self, path: str, update_fn: Callable[[Any], Any],
                    max_attempts: int = 5) -> Any:
        """Atomically read-modify-write the value at *path*.

        *update_fn* receives the current value and must return the new value.
        If a concurrent write causes a conflict the function is retried up to
        *max_attempts* times.  Returns the committed value.

        Example::

            db.transaction("/counters/visits", lambda n: (n or 0) + 1)
        """
        for attempt in range(max_attempts):
            with self._lock:
                current = self.get(path)
                new_value = update_fn(current)
                # Simple optimistic check: re-read under lock and compare
                if self.get(path) == current:
                    self.set(path, new_value)
                    return new_value
                # conflict — retry (though under RLock this shouldn't happen
                # in single-process use; the retry path is here for correctness)
        # last attempt outside the loop
        with self._lock:
            current = self.get(path)
            new_value = update_fn(current)
            self.set(path, new_value)
            return new_value

    # ---- presence / onDisconnect stub ------------------------------------

    def on_disconnect(self, path: str) -> OnDisconnect:
        """Return an :class:`OnDisconnect` handler for *path*.

        Register operations that should run when a client disconnects. In this
        local emulator the operations are triggered via
        :meth:`simulate_disconnect`.
        """
        handler = self._disconnect_handlers.setdefault(path, OnDisconnect(self, path))
        return handler

    def simulate_disconnect(self, path: Optional[str] = None) -> None:
        """Trigger registered onDisconnect operations.

        If *path* is given, only the handler for that path is triggered.
        Otherwise all registered handlers are triggered (simulating a full
        client disconnect).
        """
        if path is not None:
            handler = self._disconnect_handlers.get(path)
            if handler:
                handler.trigger()
        else:
            for handler in list(self._disconnect_handlers.values()):
                handler.trigger()
