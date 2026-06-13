"""Realtime-Database-style JSON tree.

A single JSON document addressed by slash-separated paths, supporting
``get`` / ``set`` / ``update`` / ``push`` / ``delete`` at any path. This is a
compatible SUBSET of the realtime-tree semantics.
"""

from __future__ import annotations

import threading
import time
from typing import Any, List, Optional

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


class RealtimeDatabase:
    """A single mutable JSON tree persisted as one document."""

    def __init__(self, store: Optional[BaseStore] = None) -> None:
        self._store = store if store is not None else MemoryStore()
        self._lock = threading.RLock()

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
