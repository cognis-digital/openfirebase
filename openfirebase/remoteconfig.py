"""Remote Config — local emulator.

Models the Firebase Remote Config concept:
* **Parameters** — named key/value pairs each with a default value and an
  optional list of conditional overrides.
* **Conditions** — named predicates evaluated against a *client context*
  (a dict with ``app_version``, ``platform``, ``user_id``, custom keys …).
* **fetch/evaluate** — fetch the config for a given client context; conditions
  are tested in declaration order and the first matching override wins.

Parameter values are always strings (matching the Firebase SDK); callers that
need typed values parse them with ``json.loads`` or a convenience helper.

This is a local development helper only.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .storage import BaseStore, MemoryStore

_NS_PARAMS = "remoteconfig::params"
_NS_CONDITIONS = "remoteconfig::conditions"
_NS_META = "remoteconfig::meta"

_VERSION_KEY = "_version"


class RemoteConfigError(Exception):
    """Raised for invalid Remote Config operations."""


# ---------------------------------------------------------------------------
# Condition evaluators
# ---------------------------------------------------------------------------

_CONDITION_OPS = {
    "==":   lambda a, b: str(a) == str(b),
    "!=":   lambda a, b: str(a) != str(b),
    "contains": lambda a, b: str(b).lower() in str(a).lower(),
    "startsWith": lambda a, b: str(a).lower().startswith(str(b).lower()),
    "matches": lambda a, b: __import__("re").search(b, str(a)) is not None,
}


def _eval_condition(condition: Dict[str, Any],
                    client_ctx: Dict[str, Any]) -> bool:
    """Evaluate a single condition dict against a client context.

    Condition schema::

        {
            "field":  str,      # e.g. "platform", "app_version", "user_id"
            "op":     str,      # ==, !=, contains, startsWith, matches
            "value":  str,
        }

    Returns True if the condition matches.
    """
    field = condition.get("field", "")
    op = condition.get("op", "==")
    expected = condition.get("value", "")
    actual = client_ctx.get(field, "")
    fn = _CONDITION_OPS.get(op)
    if fn is None:
        return False
    return fn(actual, expected)


def _eval_condition_group(group: Dict[str, Any],
                          client_ctx: Dict[str, Any]) -> bool:
    """Evaluate a condition group.

    Schema::

        {
            "name":       str,   # human-readable label
            "expression": [      # list of sub-conditions, AND-ed together
                {"field":…, "op":…, "value":…},
                …
            ]
        }
    """
    expressions = group.get("expression", [])
    if not expressions:
        return True   # empty = always-true condition
    return all(_eval_condition(e, client_ctx) for e in expressions)


# ---------------------------------------------------------------------------
# RemoteConfig service
# ---------------------------------------------------------------------------

class RemoteConfig:
    """Local Remote Config store.

    Parameters
    ----------
    store:
        Shared BaseStore.  If None an in-memory store is used.
    """

    def __init__(self, store: Optional[BaseStore] = None) -> None:
        self._store = store if store is not None else MemoryStore()
        # Ensure version counter exists
        if self._store.get(_NS_META, _VERSION_KEY) is None:
            self._store.set(_NS_META, _VERSION_KEY, 1)

    # ---- conditions ---------------------------------------------------------

    def set_condition(self, name: str, expression: List[Dict[str, Any]]) -> None:
        """Define a named condition.

        Parameters
        ----------
        name:
            Unique condition name, e.g. ``"ios_users"``.
        expression:
            List of sub-condition dicts (AND-ed).  Each has
            ``{"field": str, "op": str, "value": str}``.
        """
        self._store.set(_NS_CONDITIONS, name, {
            "name": name,
            "expression": expression,
        })
        self._bump_version()

    def get_condition(self, name: str) -> Optional[Dict[str, Any]]:
        return self._store.get(_NS_CONDITIONS, name)

    def delete_condition(self, name: str) -> bool:
        ok = self._store.delete(_NS_CONDITIONS, name)
        if ok:
            self._bump_version()
        return ok

    def list_conditions(self) -> List[Dict[str, Any]]:
        return [v for _, v in self._store.items(_NS_CONDITIONS)]

    # ---- parameters ---------------------------------------------------------

    def set_parameter(self, key: str, default_value: str,
                      conditional_values: Optional[List[Dict[str, Any]]] = None
                      ) -> None:
        """Define a parameter.

        Parameters
        ----------
        key:
            Parameter key, e.g. ``"welcome_message"``.
        default_value:
            Default string value used when no condition matches.
        conditional_values:
            List of ``{"condition": str, "value": str}`` overrides applied
            in order (first match wins).
        """
        self._store.set(_NS_PARAMS, key, {
            "key": key,
            "default_value": str(default_value),
            "conditional_values": conditional_values or [],
        })
        self._bump_version()

    def get_parameter(self, key: str) -> Optional[Dict[str, Any]]:
        return self._store.get(_NS_PARAMS, key)

    def delete_parameter(self, key: str) -> bool:
        ok = self._store.delete(_NS_PARAMS, key)
        if ok:
            self._bump_version()
        return ok

    def list_parameters(self) -> List[Dict[str, Any]]:
        return [v for _, v in self._store.items(_NS_PARAMS)]

    # ---- fetch / evaluate ---------------------------------------------------

    def fetch(self, client_ctx: Optional[Dict[str, Any]] = None
              ) -> Dict[str, str]:
        """Return the resolved config for *client_ctx*.

        For each parameter the conditional values list is tested in order;
        the value of the first matching condition is used.  Falls back to
        ``default_value``.

        Returns a flat ``{key: value}`` dict where all values are strings.
        """
        client_ctx = client_ctx or {}
        result: Dict[str, str] = {}
        for _, param in self._store.items(_NS_PARAMS):
            key = param["key"]
            value = param["default_value"]
            for cv in param.get("conditional_values", []):
                cond_name = cv.get("condition", "")
                cond = self._store.get(_NS_CONDITIONS, cond_name)
                if cond and _eval_condition_group(cond, client_ctx):
                    value = cv.get("value", value)
                    break
            result[key] = value
        return result

    def evaluate(self, key: str,
                 client_ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Return the resolved value for a single parameter *key*.

        Returns None if the parameter does not exist.
        """
        param = self._store.get(_NS_PARAMS, key)
        if param is None:
            return None
        client_ctx = client_ctx or {}
        value = param["default_value"]
        for cv in param.get("conditional_values", []):
            cond_name = cv.get("condition", "")
            cond = self._store.get(_NS_CONDITIONS, cond_name)
            if cond and _eval_condition_group(cond, client_ctx):
                value = cv.get("value", value)
                break
        return value

    # ---- version / metadata -------------------------------------------------

    def get_version(self) -> int:
        return self._store.get(_NS_META, _VERSION_KEY) or 1

    def _bump_version(self) -> None:
        v = self.get_version()
        self._store.set(_NS_META, _VERSION_KEY, v + 1)

    def get_template(self) -> Dict[str, Any]:
        """Return the full template (conditions + parameters + version)."""
        return {
            "version": self.get_version(),
            "conditions": self.list_conditions(),
            "parameters": {p["key"]: p for p in self.list_parameters()},
            "fetched_at": time.time(),
        }
