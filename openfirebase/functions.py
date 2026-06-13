"""Cloud-Functions-style trigger runner — deep implementation.

Register Python callables as triggers and fire them in response to:

* Database events — ``onCreate`` / ``onWrite`` / ``onUpdate`` / ``onDelete``
  (Firestore **and** RTDB paths)
* Auth events — ``onUserCreate`` / ``onUserDelete``
* Storage events — ``onObjectFinalize`` / ``onObjectDelete``
* HTTP / callable / onRequest handlers
* Pub/Sub messages (``onMessage``)
* Scheduled jobs (cron-style, executed on demand or by the schedule runner)

Handlers run synchronously in-process. Errors in one handler are isolated
and collected rather than aborting the whole dispatch.

New in messaging+compute pass
------------------------------
* ``callable`` handler type (data/context envelope, error returns ``{error:{...}}``).
* ``onAuthUserCreate`` / ``onAuthUserDelete`` Auth triggers.
* ``onStorageObjectFinalize`` / ``onStorageObjectDelete`` Storage triggers.
* ``onPubSubMessage`` Pub/Sub triggers + ``publish`` helper.
* ``schedule`` decorator + ``run_scheduled`` runner.
* ``FunctionError`` typed error for callable functions.
* ``list_callable_handlers`` / ``list_pubsub_handlers`` / ``list_scheduled``
  introspection.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

# DB event types (unchanged from v1)
ON_CREATE = "onCreate"
ON_WRITE = "onWrite"
ON_UPDATE = "onUpdate"
ON_DELETE = "onDelete"
ON_REQUEST = "onRequest"

# Auth event types
ON_AUTH_USER_CREATE = "onAuthUserCreate"
ON_AUTH_USER_DELETE = "onAuthUserDelete"

# Storage event types
ON_STORAGE_FINALIZE = "onStorageObjectFinalize"
ON_STORAGE_DELETE = "onStorageObjectDelete"

# Pub/Sub event type
ON_PUBSUB_MESSAGE = "onPubSubMessage"

# Scheduled event type
ON_SCHEDULE = "onSchedule"

_VALID_DB_EVENTS = {ON_CREATE, ON_WRITE, ON_UPDATE, ON_DELETE}
_VALID_AUTH_EVENTS = {ON_AUTH_USER_CREATE, ON_AUTH_USER_DELETE}
_VALID_STORAGE_EVENTS = {ON_STORAGE_FINALIZE, ON_STORAGE_DELETE}


class FunctionError(Exception):
    """Structured error for callable functions (message + optional code)."""

    def __init__(self, message: str, code: str = "internal") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class FunctionRegistry:
    """Holds registered handlers and dispatches events to them."""

    def __init__(self) -> None:
        # db handlers: list of (event, path_prefix, fn)
        self._db_handlers: List[tuple] = []
        # http (onRequest) handlers: name -> fn
        self._http_handlers: Dict[str, Callable] = {}
        # callable handlers: name -> fn  (called with (data, context))
        self._callable_handlers: Dict[str, Callable] = {}
        # auth handlers: list of (event, fn)
        self._auth_handlers: List[tuple] = []
        # storage handlers: list of (event, bucket_prefix, fn)
        self._storage_handlers: List[tuple] = []
        # pubsub handlers: topic -> list of fn
        self._pubsub_handlers: Dict[str, List[Callable]] = {}
        # scheduled handlers: list of {"name", "schedule", "fn", "last_run", "lock"}
        self._scheduled: List[Dict[str, Any]] = []
        self._errors: List[Dict[str, Any]] = []

    # ---- DB registration ---------------------------------------------------
    def on_db(self, event: str, path_prefix: str = "") -> Callable:
        if event not in _VALID_DB_EVENTS:
            raise ValueError(f"invalid db event: {event!r}")

        def decorator(fn: Callable) -> Callable:
            self._db_handlers.append((event, path_prefix, fn))
            return fn

        return decorator

    def register_db(self, event: str, path_prefix: str, fn: Callable) -> None:
        if event not in _VALID_DB_EVENTS:
            raise ValueError(f"invalid db event: {event!r}")
        self._db_handlers.append((event, path_prefix, fn))

    # ---- HTTP / request registration ----------------------------------------
    def on_request(self, name: str) -> Callable:
        def decorator(fn: Callable) -> Callable:
            self._http_handlers[name] = fn
            return fn

        return decorator

    def register_request(self, name: str, fn: Callable) -> None:
        self._http_handlers[name] = fn

    # ---- Callable registration ----------------------------------------------
    def on_call(self, name: str) -> Callable:
        """Decorator for callable functions.

        The handler receives ``(data, context)`` where:
        * ``data``    — arbitrary JSON-decoded payload from the caller.
        * ``context`` — dict with ``{"name": name, "auth": None, ...}``.

        Raise :class:`FunctionError` to return a structured error to the caller.
        Return any JSON-serialisable value.
        """
        def decorator(fn: Callable) -> Callable:
            self._callable_handlers[name] = fn
            return fn

        return decorator

    def register_callable(self, name: str, fn: Callable) -> None:
        self._callable_handlers[name] = fn

    # ---- Auth event registration --------------------------------------------
    def on_auth_user(self, event: str) -> Callable:
        if event not in _VALID_AUTH_EVENTS:
            raise ValueError(f"invalid auth event: {event!r}")

        def decorator(fn: Callable) -> Callable:
            self._auth_handlers.append((event, fn))
            return fn

        return decorator

    def register_auth(self, event: str, fn: Callable) -> None:
        if event not in _VALID_AUTH_EVENTS:
            raise ValueError(f"invalid auth event: {event!r}")
        self._auth_handlers.append((event, fn))

    # ---- Storage event registration -----------------------------------------
    def on_storage(self, event: str, bucket_prefix: str = "") -> Callable:
        if event not in _VALID_STORAGE_EVENTS:
            raise ValueError(f"invalid storage event: {event!r}")

        def decorator(fn: Callable) -> Callable:
            self._storage_handlers.append((event, bucket_prefix, fn))
            return fn

        return decorator

    def register_storage(self, event: str, bucket_prefix: str, fn: Callable) -> None:
        if event not in _VALID_STORAGE_EVENTS:
            raise ValueError(f"invalid storage event: {event!r}")
        self._storage_handlers.append((event, bucket_prefix, fn))

    # ---- Pub/Sub registration -----------------------------------------------
    def on_pubsub(self, topic: str) -> Callable:
        """Decorator for Pub/Sub message handlers on ``topic``."""
        def decorator(fn: Callable) -> Callable:
            self._pubsub_handlers.setdefault(topic, []).append(fn)
            return fn

        return decorator

    def register_pubsub(self, topic: str, fn: Callable) -> None:
        self._pubsub_handlers.setdefault(topic, []).append(fn)

    # ---- Scheduled handlers -------------------------------------------------
    def schedule(self, name: str, cron: str = "") -> Callable:
        """Decorator for scheduled functions.

        ``cron`` is stored for documentation but is **not evaluated** here —
        call :meth:`run_scheduled` to fire a scheduled function on demand.
        """
        def decorator(fn: Callable) -> Callable:
            self._scheduled.append({
                "name": name,
                "schedule": cron,
                "fn": fn,
                "last_run": None,
                "lock": threading.Lock(),
            })
            return fn

        return decorator

    def register_schedule(self, name: str, fn: Callable, cron: str = "") -> None:
        self._scheduled.append({
            "name": name,
            "schedule": cron,
            "fn": fn,
            "last_run": None,
            "lock": threading.Lock(),
        })

    # ---- Dispatch -----------------------------------------------------------
    def dispatch_db(self, event: str, path: str, before: Any, after: Any) -> List[Any]:
        """Fire all db handlers matching ``event`` and ``path`` prefix."""
        results: List[Any] = []
        ctx = {"event": event, "path": path, "before": before, "after": after}
        for h_event, prefix, fn in self._db_handlers:
            fires = (h_event == event) or (
                h_event == ON_WRITE and event in _VALID_DB_EVENTS
            )
            if not fires:
                continue
            if prefix and not path.startswith(prefix):
                continue
            try:
                results.append(fn(dict(ctx)))
            except Exception as exc:
                self._errors.append({"handler": getattr(fn, "__name__", str(fn)),
                                     "error": str(exc), "path": path})
        return results

    def dispatch_auth(self, event: str, user: Dict[str, Any]) -> List[Any]:
        """Fire all auth handlers matching ``event``."""
        results: List[Any] = []
        for h_event, fn in self._auth_handlers:
            if h_event != event:
                continue
            try:
                results.append(fn(dict(user)))
            except Exception as exc:
                self._errors.append({"handler": getattr(fn, "__name__", str(fn)),
                                     "error": str(exc), "event": event})
        return results

    def dispatch_storage(self, event: str, object_meta: Dict[str, Any]) -> List[Any]:
        """Fire all storage handlers matching ``event`` (and optional bucket prefix)."""
        results: List[Any] = []
        bucket = object_meta.get("bucket", "")
        for h_event, bucket_prefix, fn in self._storage_handlers:
            if h_event != event:
                continue
            if bucket_prefix and not bucket.startswith(bucket_prefix):
                continue
            try:
                results.append(fn(dict(object_meta)))
            except Exception as exc:
                self._errors.append({"handler": getattr(fn, "__name__", str(fn)),
                                     "error": str(exc), "event": event})
        return results

    def publish(self, topic: str, message: Any) -> List[Any]:
        """Publish a message to ``topic``; fires all registered subscribers."""
        results: List[Any] = []
        subscribers = self._pubsub_handlers.get(topic, [])
        ctx = {"topic": topic, "message": message, "published_at": time.time()}
        for fn in subscribers:
            try:
                results.append(fn(dict(ctx)))
            except Exception as exc:
                self._errors.append({"handler": getattr(fn, "__name__", str(fn)),
                                     "error": str(exc), "topic": topic})
        return results

    def call_request(self, name: str, request: Dict[str, Any]) -> Any:
        fn = self._http_handlers.get(name)
        if fn is None:
            raise KeyError(f"no http function named {name!r}")
        return fn(request)

    def call_callable(self, name: str, data: Any,
                      context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Invoke a callable function; returns ``{"result": ...}`` or ``{"error": {...}}``."""
        fn = self._callable_handlers.get(name)
        if fn is None:
            raise KeyError(f"no callable function named {name!r}")
        ctx = context or {}
        ctx.setdefault("name", name)
        try:
            result = fn(data, ctx)
            return {"result": result}
        except FunctionError as exc:
            return {"error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            self._errors.append({"handler": name, "error": str(exc)})
            return {"error": {"code": "internal", "message": str(exc)}}

    def run_scheduled(self, name: str) -> Any:
        """Run a scheduled function by name.  Returns the handler's return value."""
        for entry in self._scheduled:
            if entry["name"] == name:
                with entry["lock"]:
                    try:
                        result = entry["fn"]({"scheduled_time": time.time()})
                        entry["last_run"] = time.time()
                        return result
                    except Exception as exc:
                        self._errors.append({"handler": name, "error": str(exc)})
                        raise
        raise KeyError(f"no scheduled function named {name!r}")

    # ---- Introspection ------------------------------------------------------
    def list_db_handlers(self) -> List[Dict[str, str]]:
        return [{"event": e, "path_prefix": p, "name": getattr(f, "__name__", str(f))}
                for e, p, f in self._db_handlers]

    def list_http_handlers(self) -> List[str]:
        return sorted(self._http_handlers.keys())

    def list_callable_handlers(self) -> List[str]:
        return sorted(self._callable_handlers.keys())

    def list_auth_handlers(self) -> List[Dict[str, str]]:
        return [{"event": e, "name": getattr(f, "__name__", str(f))}
                for e, f in self._auth_handlers]

    def list_storage_handlers(self) -> List[Dict[str, str]]:
        return [{"event": e, "bucket_prefix": b, "name": getattr(f, "__name__", str(f))}
                for e, b, f in self._storage_handlers]

    def list_pubsub_handlers(self) -> Dict[str, List[str]]:
        return {
            topic: [getattr(f, "__name__", str(f)) for f in fns]
            for topic, fns in self._pubsub_handlers.items()
        }

    def list_scheduled(self) -> List[Dict[str, Any]]:
        return [
            {"name": e["name"], "schedule": e["schedule"],
             "last_run": e["last_run"]}
            for e in self._scheduled
        ]

    @property
    def errors(self) -> List[Dict[str, Any]]:
        return list(self._errors)


# Module-level convenience registry + decorators
_default_registry: Optional[FunctionRegistry] = None


def default_registry() -> FunctionRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = FunctionRegistry()
    return _default_registry


def trigger(event: str, path_prefix: str = "") -> Callable:
    """Decorator registering a db trigger on the default registry."""
    return default_registry().on_db(event, path_prefix)
