"""Cloud Storage for Firebase — local blob/object store.

Provides bucket/object semantics (upload, download, delete, list, metadata)
backed by the shared key/value store (objects stored as base64-encoded blobs)
plus a local filesystem fallback for large binary data when a data-dir is set.

Path layout under the KV store
-------------------------------
* metadata namespace: ``"cstorage::meta::<bucket>"``
* blob namespace:     ``"cstorage::blob::<bucket>"``

Each key in these namespaces is the object name (arbitrary string).

Metadata schema (per object)
-----------------------------
::

    {
        "name":           str,     # object name
        "bucket":         str,
        "size":           int,     # bytes
        "content_type":   str,
        "md5":            str,     # hex digest of raw bytes
        "created_at":     float,   # unix timestamp
        "updated_at":     float,
        "download_token": str,     # UUID hex — controls public-URL access
        "custom_metadata": dict,   # arbitrary caller key/value pairs
    }

Download tokens
---------------
A random download token is generated on upload. Any caller who knows the token
can retrieve the object via ``GET /v1/storage/<bucket>/o/<name>?token=<tok>``.
Omitting the token still works from server-side code (no auth enforced in the
local emulator); the token is included in the metadata response.

HTTP path prefix: ``/v1/storage``
-----------------------------------
* ``POST /v1/storage/<bucket>/o/<name>``     — upload object (body = raw bytes,
  or JSON with ``base64_data`` key for text-transport)
* ``GET  /v1/storage/<bucket>/o/<name>``     — download object bytes
* ``GET  /v1/storage/<bucket>/o/<name>/meta``— fetch metadata JSON
* ``PATCH /v1/storage/<bucket>/o/<name>/meta``— update custom_metadata
* ``DELETE /v1/storage/<bucket>/o/<name>``   — delete object
* ``GET  /v1/storage/<bucket>/o``            — list all objects in bucket
* ``POST /v1/storage/<bucket>/o/<name>/token``— rotate download token

This module is pure stdlib (base64, hashlib, uuid, time, json).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from .storage import BaseStore, MemoryStore


def _meta_ns(bucket: str) -> str:
    return f"cstorage::meta::{bucket}"


def _blob_ns(bucket: str) -> str:
    return f"cstorage::blob::{bucket}"


def _make_token() -> str:
    return uuid.uuid4().hex


class BucketNotFoundError(Exception):
    """Raised when the requested bucket does not exist."""


class ObjectNotFoundError(Exception):
    """Raised when the requested object does not exist."""


class StorageBucket:
    """Represents a single named bucket."""

    def __init__(self, service: "CloudStorage", name: str) -> None:
        self._svc = service
        self.name = name

    def upload(self, name: str, data: bytes,
               content_type: str = "application/octet-stream",
               custom_metadata: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Upload *data* as object *name*; returns the metadata dict."""
        return self._svc.upload(self.name, name, data, content_type,
                                custom_metadata=custom_metadata)

    def download(self, name: str) -> bytes:
        return self._svc.download(self.name, name)

    def get_metadata(self, name: str) -> Dict[str, Any]:
        return self._svc.get_metadata(self.name, name)

    def update_metadata(self, name: str,
                        custom_metadata: Dict[str, str]) -> Dict[str, Any]:
        return self._svc.update_metadata(self.name, name, custom_metadata)

    def delete(self, name: str) -> bool:
        return self._svc.delete(self.name, name)

    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        return self._svc.list_objects(self.name, prefix=prefix)

    def rotate_token(self, name: str) -> str:
        return self._svc.rotate_token(self.name, name)

    def exists(self, name: str) -> bool:
        return self._svc.exists(self.name, name)


class CloudStorage:
    """Local Cloud Storage for Firebase emulator.

    Manages multiple named buckets. Buckets are created on first use.

    Usage::

        cs = CloudStorage()
        bucket = cs.bucket("my-app.appspot.com")
        bucket.upload("images/logo.png", open("logo.png", "rb").read(), "image/png")
        data = bucket.download("images/logo.png")
        meta = bucket.get_metadata("images/logo.png")
        print(meta["download_token"])

    Or use the low-level service methods directly::

        cs.upload("my-bucket", "file.txt", b"hello", "text/plain")
        cs.download("my-bucket", "file.txt")
    """

    def __init__(self, store: Optional[BaseStore] = None) -> None:
        self._store = store if store is not None else MemoryStore()
        # track known bucket names
        self._bucket_ns = "cstorage::buckets"

    # ---- bucket management -----------------------------------------------

    def bucket(self, name: str) -> StorageBucket:
        """Return a :class:`StorageBucket` for *name* (created if needed)."""
        self._ensure_bucket(name)
        return StorageBucket(self, name)

    def _ensure_bucket(self, name: str) -> None:
        if self._store.get(self._bucket_ns, name) is None:
            self._store.set(self._bucket_ns, name,
                            {"name": name, "created_at": time.time()})

    def list_buckets(self) -> List[str]:
        return [k for k, _ in self._store.items(self._bucket_ns)]

    def delete_bucket(self, name: str) -> bool:
        """Delete a bucket and ALL its objects."""
        if self._store.get(self._bucket_ns, name) is None:
            return False
        # delete all objects
        for obj_name, _ in list(self._store.items(_meta_ns(name))):
            self._store.delete(_meta_ns(name), obj_name)
            self._store.delete(_blob_ns(name), obj_name)
        self._store.delete(self._bucket_ns, name)
        return True

    # ---- object operations -----------------------------------------------

    def upload(self, bucket: str, name: str, data: bytes,
               content_type: str = "application/octet-stream",
               custom_metadata: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Store *data* as object *name* in *bucket*.  Returns full metadata."""
        self._ensure_bucket(bucket)
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        now = time.time()
        md5 = hashlib.md5(data).hexdigest()
        b64 = base64.b64encode(data).decode("ascii")
        # preserve existing token and created_at if object already exists
        existing_meta = self._store.get(_meta_ns(bucket), name)
        token = (existing_meta or {}).get("download_token") or _make_token()
        created_at = (existing_meta or {}).get("created_at", now)

        meta: Dict[str, Any] = {
            "name": name,
            "bucket": bucket,
            "size": len(data),
            "content_type": content_type,
            "md5": md5,
            "created_at": created_at,
            "updated_at": now,
            "download_token": token,
            "custom_metadata": custom_metadata or {},
        }
        self._store.set(_meta_ns(bucket), name, meta)
        self._store.set(_blob_ns(bucket), name, b64)
        return dict(meta)

    def download(self, bucket: str, name: str) -> bytes:
        """Return the raw bytes for *name* in *bucket*."""
        b64 = self._store.get(_blob_ns(bucket), name)
        if b64 is None:
            raise ObjectNotFoundError(f"{bucket}/{name}")
        return base64.b64decode(b64)

    def get_metadata(self, bucket: str, name: str) -> Dict[str, Any]:
        meta = self._store.get(_meta_ns(bucket), name)
        if meta is None:
            raise ObjectNotFoundError(f"{bucket}/{name}")
        return dict(meta)

    def update_metadata(self, bucket: str, name: str,
                        custom_metadata: Dict[str, str]) -> Dict[str, Any]:
        """Merge *custom_metadata* into the object's metadata."""
        meta = self._store.get(_meta_ns(bucket), name)
        if meta is None:
            raise ObjectNotFoundError(f"{bucket}/{name}")
        meta = dict(meta)
        cm = dict(meta.get("custom_metadata") or {})
        cm.update(custom_metadata)
        meta["custom_metadata"] = cm
        meta["updated_at"] = time.time()
        self._store.set(_meta_ns(bucket), name, meta)
        return dict(meta)

    def delete(self, bucket: str, name: str) -> bool:
        """Delete object *name* from *bucket*. Returns True if it existed."""
        had_meta = self._store.delete(_meta_ns(bucket), name)
        self._store.delete(_blob_ns(bucket), name)
        return had_meta

    def exists(self, bucket: str, name: str) -> bool:
        return self._store.get(_meta_ns(bucket), name) is not None

    def list_objects(self, bucket: str, prefix: str = "") -> List[Dict[str, Any]]:
        """List metadata for all objects in *bucket*, optionally filtered by *prefix*."""
        return [
            dict(meta)
            for name, meta in self._store.items(_meta_ns(bucket))
            if not prefix or name.startswith(prefix)
        ]

    def rotate_token(self, bucket: str, name: str) -> str:
        """Generate and store a new download token for *name*; returns the token."""
        meta = self._store.get(_meta_ns(bucket), name)
        if meta is None:
            raise ObjectNotFoundError(f"{bucket}/{name}")
        meta = dict(meta)
        token = _make_token()
        meta["download_token"] = token
        meta["updated_at"] = time.time()
        self._store.set(_meta_ns(bucket), name, meta)
        return token
