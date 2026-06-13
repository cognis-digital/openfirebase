"""Cloud-Functions-style trigger runner.

Register Python callables as triggers and fire them in response to database
events (``onCreate`` / ``onWrite`` / ``onDelete``) or HTTP requests
(``onRequest``). Handlers run synchronously in-process. Errors in one handler
are isolated and collected rather than aborting the whole dispatch.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# Event types
ON_CREATE = "onCreate"
ON_WRITE = "onWrite"
ON_UPDATE = "onUpdate"
ON_DELETE = "onDelete"
ON_REQUEST = "onRequest"

_VALID_DB_EVENTS = {ON_CREATE, ON_WRITE, ON_UPDATE, ON_DELETE}


class FunctionRegistry:
    """Holds registered handlers and dispatches events to them."""

    def __init__(self) -> None:
        # db handlers: list of (event, path_prefix, fn)
        self._db_handlers: List[tuple] = []
        # http handlers: name -> fn
        self._http_handlers: Dict[str, Callable] = {}
        self._errors: List[Dict[str, Any]] = []

    # ---- registration -----------------------------------------------------
    def on_db(self, event: str, path_prefix: str = "") -> Callable:
        if event not in _VALID_DB_EVENTS:
            raise ValueError(f"invalid db event: {event!r}")

        def decorator(fn: Callable) -> Callable:
            self._db_handlers.append((event, path_prefix, fn))
            return fn

        return decorator

    def on_request(self, name: str) -> Callable:
        def decorator(fn: Callable) -> Callable:
            self._http_handlers[name] = fn
            return fn

        return decorator

    def register_request(self, name: str, fn: Callable) -> None:
        self._http_handlers[name] = fn

    def register_db(self, event: str, path_prefix: str, fn: Callable) -> None:
        if event not in _VALID_DB_EVENTS:
            raise ValueError(f"invalid db event: {event!r}")
        self._db_handlers.append((event, path_prefix, fn))

    # ---- dispatch ---------------------------------------------------------
    def dispatch_db(self, event: str, path: str, before: Any, after: Any) -> List[Any]:
        """Fire all db handlers matching ``event`` and ``path`` prefix.

        ``onWrite`` handlers fire for create, update, and delete. Returns the
        list of handler return values (successful ones).
        """
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
            except Exception as exc:  # isolate handler failures
                self._errors.append({"handler": getattr(fn, "__name__", str(fn)),
                                     "error": str(exc), "path": path})
        return results

    def call_request(self, name: str, request: Dict[str, Any]) -> Any:
        fn = self._http_handlers.get(name)
        if fn is None:
            raise KeyError(f"no http function named {name!r}")
        return fn(request)

    # ---- introspection ----------------------------------------------------
    def list_db_handlers(self) -> List[Dict[str, str]]:
        return [{"event": e, "path_prefix": p, "name": getattr(f, "__name__", str(f))}
                for e, p, f in self._db_handlers]

    def list_http_handlers(self) -> List[str]:
        return sorted(self._http_handlers.keys())

    @property
    def errors(self) -> List[Dict[str, Any]]:
        return list(self._errors)


# Module-level convenience registry + decorator (mirrors functions.https-style)
_default_registry: Optional[FunctionRegistry] = None


def default_registry() -> FunctionRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = FunctionRegistry()
    return _default_registry


def trigger(event: str, path_prefix: str = "") -> Callable:
    """Decorator registering a db trigger on the default registry."""
    return default_registry().on_db(event, path_prefix)
