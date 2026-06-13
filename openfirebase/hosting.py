"""Static Hosting service — deep implementation.

Serves static files from a local public directory, with:

* Directory-index resolution (``/`` -> ``index.html``)
* SPA-style fallback to ``index.html`` when configured
* Path traversal outside the root is rejected

New in messaging+compute pass
------------------------------
* **Rewrites** — map URL patterns to local paths (or function names for
  transparent proxy stubs).
* **Redirects** — map URL patterns to target URLs / paths with optional status
  (301/302/307/308).
* **Headers** — inject custom HTTP response headers per glob pattern.
* **Preview channels** — named channel overlay directories; a channel's files
  shadow the main public dir without replacing it.

Configuration is via a ``firebase_hosting_config`` dict (or equivalent kwargs)
that mirrors the relevant subset of ``firebase.json`` ``hosting:`` block::

    {
        "rewrites": [
            {"source": "/api/**", "function": "myApiFunction"},
            {"source": "/legacy", "destination": "/new-path"},
        ],
        "redirects": [
            {"source": "/old", "destination": "/new", "type": 301},
        ],
        "headers": [
            {"source": "**/*.html", "headers": [{"key": "X-Frame-Options", "value": "DENY"}]},
            {"source": "/api/**",   "headers": [{"key": "Cache-Control",   "value": "no-store"}]},
        ],
    }

Pattern matching uses simple glob rules (``*`` = one path segment,
``**`` = any number of path segments / characters).
"""

from __future__ import annotations

import fnmatch
import mimetypes
import os
import re
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Convert a hosting source glob pattern to a compiled regex.

    ``**`` matches any sequence of characters (including ``/``).
    ``*`` matches any sequence of characters **not** including ``/``.
    """
    # Escape everything, then replace placeholder tokens for * and **
    escaped = re.escape(pattern)
    # re.escape turns ** → \\*\\*  and  * → \\*
    escaped = escaped.replace(r"\*\*", "__DOUBLE_STAR__")
    escaped = escaped.replace(r"\*", "[^/]*")
    escaped = escaped.replace("__DOUBLE_STAR__", ".*")
    return re.compile(f"^{escaped}$")


def _pattern_matches(pattern: str, path: str) -> bool:
    """Return True if ``path`` matches the hosting glob ``pattern``."""
    # Ensure path starts with / for matching
    if not path.startswith("/"):
        path = "/" + path
    try:
        return bool(_glob_to_regex(pattern).match(path))
    except re.error:
        return fnmatch.fnmatch(path, pattern)


class Hosting:
    """Resolve request paths to files under a public root.

    Parameters
    ----------
    public_dir:
        Root directory for static files.
    spa_fallback:
        If True, unresolved paths fall back to ``index.html``.
    rewrites:
        List of ``{"source": str, "destination": str|None,
        "function": str|None}`` rules.  ``destination`` rewrites the path
        before resolution; ``function`` stores the function name (returned as
        metadata for the server to proxy).
    redirects:
        List of ``{"source": str, "destination": str, "type": int}`` rules.
        ``type`` defaults to 301.
    headers:
        List of ``{"source": str, "headers": [{"key": str, "value": str}]}``
        rules.  All matching rules are merged (later rules win on key
        collision).
    """

    def __init__(
        self,
        public_dir: str,
        *,
        spa_fallback: bool = False,
        rewrites: Optional[List[Dict[str, Any]]] = None,
        redirects: Optional[List[Dict[str, Any]]] = None,
        headers: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.public_dir = os.path.abspath(public_dir)
        self.spa_fallback = spa_fallback
        self.rewrites: List[Dict[str, Any]] = rewrites or []
        self.redirects: List[Dict[str, Any]] = redirects or []
        self.headers_rules: List[Dict[str, Any]] = headers or []
        # preview channels: name -> overlay_dir
        self._channels: Dict[str, str] = {}

    # ---- Preview channels ---------------------------------------------------
    def create_channel(self, name: str, overlay_dir: Optional[str] = None) -> str:
        """Register a preview channel.

        ``overlay_dir`` is used as the file root for this channel (shadows the
        main ``public_dir``).  If not given a temporary directory name is
        synthesised (callers should pass an actual directory before serving
        from the channel).

        Returns the channel name.
        """
        if overlay_dir is None:
            overlay_dir = os.path.join(self.public_dir, f"__channel_{name}")
        self._channels[name] = os.path.abspath(overlay_dir)
        return name

    def delete_channel(self, name: str) -> bool:
        if name not in self._channels:
            return False
        del self._channels[name]
        return True

    def list_channels(self) -> List[Dict[str, str]]:
        return [{"name": n, "dir": d} for n, d in self._channels.items()]

    def get_channel_url(self, name: str, base_url: str = "http://localhost:8080") -> str:
        """Return a synthetic preview URL for ``name`` (for local dev only)."""
        if name not in self._channels:
            raise KeyError(f"channel {name!r} not found")
        return f"{base_url}/__channel/{name}/"

    def channel_root(self, name: str) -> Optional[str]:
        return self._channels.get(name)

    # ---- Redirect matching --------------------------------------------------
    def check_redirect(self, url_path: str) -> Optional[Dict[str, Any]]:
        """If ``url_path`` matches a redirect rule, return the rule dict.

        Returns None if no redirect applies.
        """
        for rule in self.redirects:
            source = rule.get("source", "")
            if _pattern_matches(source, url_path):
                return {
                    "destination": rule.get("destination", "/"),
                    "status": int(rule.get("type", 301)),
                }
        return None

    # ---- Rewrite matching ---------------------------------------------------
    def check_rewrite(self, url_path: str) -> Optional[Dict[str, Any]]:
        """If ``url_path`` matches a rewrite rule, return the rule dict.

        For path rewrites (``destination``), returns ``{"rewritten_path": str}``.
        For function rewrites (``function``), returns ``{"function": str}``.
        Returns None if no rewrite applies.
        """
        for rule in self.rewrites:
            source = rule.get("source", "")
            if _pattern_matches(source, url_path):
                if "function" in rule:
                    return {"function": rule["function"]}
                if "destination" in rule:
                    return {"rewritten_path": rule["destination"]}
        return None

    # ---- Header matching ----------------------------------------------------
    def get_extra_headers(self, url_path: str) -> Dict[str, str]:
        """Return merged custom headers for ``url_path`` from all matching rules."""
        merged: Dict[str, str] = {}
        for rule in self.headers_rules:
            source = rule.get("source", "**")
            if _pattern_matches(source, url_path):
                for h in rule.get("headers", []):
                    merged[h["key"]] = h["value"]
        return merged

    # ---- File resolution ----------------------------------------------------
    def _safe_join(self, root: str, url_path: str) -> Optional[str]:
        rel = url_path.lstrip("/")
        candidate = os.path.abspath(os.path.join(root, rel))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate

    def resolve(self, url_path: str, *,
                channel: Optional[str] = None) -> Optional[str]:
        """Return an absolute file path for ``url_path`` or None if not found.

        If ``channel`` is given, overlay_dir for that channel is checked first.
        """
        # Apply rewrite (path destination) before resolving
        rewrite = self.check_rewrite(url_path)
        if rewrite and "rewritten_path" in rewrite:
            url_path = rewrite["rewritten_path"]

        if not url_path or url_path == "/":
            url_path = "/index.html"

        roots: List[str] = []
        if channel is not None and channel in self._channels:
            roots.append(self._channels[channel])
        roots.append(self.public_dir)

        for root in roots:
            candidate = self._safe_join(root, url_path)
            if candidate is None:
                continue
            if os.path.isdir(candidate):
                index = os.path.join(candidate, "index.html")
                if os.path.isfile(index):
                    return index
                continue
            if os.path.isfile(candidate):
                return candidate

        if self.spa_fallback:
            # try channel first, then public_dir
            for root in roots:
                index = os.path.join(root, "index.html")
                if os.path.isfile(index):
                    return index

        return None

    def serve(self, url_path: str, *,
              channel: Optional[str] = None) -> Optional[Tuple[bytes, str]]:
        """Return ``(content_bytes, content_type)`` or None.

        Maintains the original two-tuple API for backward compatibility.
        Use :meth:`serve_with_headers` to also get custom response headers.
        """
        path = self.resolve(url_path, channel=channel)
        if path is None:
            return None
        ctype, _ = mimetypes.guess_type(path)
        with open(path, "rb") as fh:
            return fh.read(), (ctype or "application/octet-stream")

    def serve_with_headers(
        self, url_path: str, *, channel: Optional[str] = None
    ) -> Optional[Tuple[bytes, str, Dict[str, str]]]:
        """Return ``(content_bytes, content_type, extra_headers)`` or None."""
        extra = self.get_extra_headers(url_path)
        result = self.serve(url_path, channel=channel)
        if result is None:
            return None
        data, ctype = result
        return data, ctype, extra
