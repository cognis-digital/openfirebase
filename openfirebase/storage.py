"""Storage backends shared by openfirebase services.

A small key/value abstraction with two implementations:

* ``MemoryStore``   - dict-backed, used for tests and ``--memory`` mode.
* ``SqliteStore``   - sqlite3-backed, used for persistent local development.

Both store JSON-serialisable values under a ``(namespace, key)`` pair and expose
the same minimal API so services do not care which backend is active.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Iterator, Optional, Tuple


class BaseStore:
    """Abstract key/value store keyed by ``(namespace, key)``."""

    def get(self, namespace: str, key: str) -> Optional[object]:
        raise NotImplementedError

    def set(self, namespace: str, key: str, value: object) -> None:
        raise NotImplementedError

    def delete(self, namespace: str, key: str) -> bool:
        raise NotImplementedError

    def items(self, namespace: str) -> Iterator[Tuple[str, object]]:
        raise NotImplementedError

    def clear(self, namespace: Optional[str] = None) -> None:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class MemoryStore(BaseStore):
    """In-memory store. Thread-safe for the simple operations we perform."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, object]] = {}
        self._lock = threading.RLock()

    def get(self, namespace: str, key: str) -> Optional[object]:
        with self._lock:
            ns = self._data.get(namespace)
            if ns is None:
                return None
            val = ns.get(key)
            return json.loads(json.dumps(val)) if val is not None else None

    def set(self, namespace: str, key: str, value: object) -> None:
        with self._lock:
            self._data.setdefault(namespace, {})[key] = json.loads(json.dumps(value))

    def delete(self, namespace: str, key: str) -> bool:
        with self._lock:
            ns = self._data.get(namespace)
            if ns and key in ns:
                del ns[key]
                return True
            return False

    def items(self, namespace: str) -> Iterator[Tuple[str, object]]:
        with self._lock:
            ns = self._data.get(namespace, {})
            # snapshot to avoid mutation-during-iteration surprises
            snapshot = [(k, json.loads(json.dumps(v))) for k, v in ns.items()]
        return iter(snapshot)

    def clear(self, namespace: Optional[str] = None) -> None:
        with self._lock:
            if namespace is None:
                self._data.clear()
            else:
                self._data.pop(namespace, None)


class SqliteStore(BaseStore):
    """Persistent store backed by a single sqlite3 file."""

    def __init__(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        # check_same_thread=False so the HTTP server threads can share it under our lock
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                namespace TEXT NOT NULL,
                key       TEXT NOT NULL,
                value     TEXT NOT NULL,
                PRIMARY KEY (namespace, key)
            )
            """
        )
        self._conn.commit()

    def get(self, namespace: str, key: str) -> Optional[object]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)
            )
            row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def set(self, namespace: str, key: str, value: object) -> None:
        payload = json.dumps(value)
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(namespace, key, value) VALUES(?,?,?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value",
                (namespace, key, payload),
            )
            self._conn.commit()

    def delete(self, namespace: str, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM kv WHERE namespace=? AND key=?", (namespace, key)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def items(self, namespace: str) -> Iterator[Tuple[str, object]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, value FROM kv WHERE namespace=?", (namespace,)
            )
            rows = cur.fetchall()
        return iter([(k, json.loads(v)) for k, v in rows])

    def clear(self, namespace: Optional[str] = None) -> None:
        with self._lock:
            if namespace is None:
                self._conn.execute("DELETE FROM kv")
            else:
                self._conn.execute("DELETE FROM kv WHERE namespace=?", (namespace,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def make_store(data_dir: Optional[str]) -> BaseStore:
    """Return a persistent store under ``data_dir`` or a MemoryStore if None."""
    if data_dir is None:
        return MemoryStore()
    return SqliteStore(os.path.join(data_dir, "openfirebase.sqlite3"))
