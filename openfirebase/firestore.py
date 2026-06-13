"""Firestore-style document database.

Provides collections of documents (JSON maps) addressed by id, with create /
get / set / update / delete and a chainable ``where`` query builder supporting
the common operators. This is a compatible SUBSET of the document-database
semantics; it is not the real product.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

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
}


def _ns(collection: str) -> str:
    return f"firestore::{collection}"


class Query:
    """A chainable query over a single collection.

    Filters are applied in Python after loading the collection. This mirrors the
    public ``where`` / ``order_by`` / ``limit`` builder shape.
    """

    def __init__(self, db: "Firestore", collection: str) -> None:
        self._db = db
        self._collection = collection
        self._filters: List[tuple] = []
        self._order: Optional[tuple] = None
        self._limit: Optional[int] = None

    def where(self, field: str, op: str, value: Any) -> "Query":
        if op not in _OPERATORS:
            raise ValueError(f"unsupported operator: {op!r}")
        self._filters.append((field, op, value))
        return self

    def order_by(self, field: str, direction: str = "asc") -> "Query":
        if direction not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")
        self._order = (field, direction)
        return self

    def limit(self, n: int) -> "Query":
        self._limit = int(n)
        return self

    def stream(self) -> List[Dict[str, Any]]:
        docs = [dict(v, id=k) for k, v in self._db._store.items(_ns(self._collection))]
        for field, op, value in self._filters:
            fn = _OPERATORS[op]
            docs = [d for d in docs if field in d and fn(d.get(field), value)]
        if self._order is not None:
            field, direction = self._order
            docs.sort(key=lambda d: (d.get(field) is None, d.get(field)),
                      reverse=(direction == "desc"))
        if self._limit is not None:
            docs = docs[: self._limit]
        return docs

    # Convenience aliases
    def get(self) -> List[Dict[str, Any]]:
        return self.stream()


class Firestore:
    """A document database of collections and documents."""

    def __init__(self, store: Optional[BaseStore] = None) -> None:
        self._store = store if store is not None else MemoryStore()

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
            merged = dict(existing)
            merged.update(data)
            payload = merged
            payload["_created_at"] = existing.get("_created_at", now)
        else:
            payload = dict(data)
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
        """Patch fields on an existing doc. Returns False if it does not exist."""
        existing = self._store.get(_ns(collection), doc_id)
        if not isinstance(existing, dict):
            return False
        merged = dict(existing)
        merged.update(data)
        merged["_updated_at"] = time.time()
        self._store.set(_ns(collection), doc_id, merged)
        return True

    def delete(self, collection: str, doc_id: str) -> bool:
        return self._store.delete(_ns(collection), doc_id)

    def exists(self, collection: str, doc_id: str) -> bool:
        return self._store.get(_ns(collection), doc_id) is not None

    # ---- collection / query ----------------------------------------------
    def collection(self, collection: str) -> Query:
        return Query(self, collection)

    def where(self, collection: str, field: str, op: str, value: Any) -> Query:
        return Query(self, collection).where(field, op, value)

    def list(self, collection: str) -> List[Dict[str, Any]]:
        return Query(self, collection).stream()

    def collections(self) -> Iterable[str]:
        # Not natively enumerable in the KV store; callers track names.
        raise NotImplementedError("collection enumeration is a roadmap item")
