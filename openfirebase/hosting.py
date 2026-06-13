"""Static Hosting service.

Serves static files from a local public directory, with directory-index
resolution (``/`` -> ``index.html``) and SPA-style fallback to ``index.html``
when configured. Path traversal outside the root is rejected.
"""

from __future__ import annotations

import mimetypes
import os
from typing import Optional, Tuple


class Hosting:
    """Resolve request paths to files under a public root."""

    def __init__(self, public_dir: str, spa_fallback: bool = False) -> None:
        self.public_dir = os.path.abspath(public_dir)
        self.spa_fallback = spa_fallback

    def _safe_join(self, url_path: str) -> Optional[str]:
        rel = url_path.lstrip("/")
        candidate = os.path.abspath(os.path.join(self.public_dir, rel))
        # Reject traversal outside the root.
        root = self.public_dir
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate

    def resolve(self, url_path: str) -> Optional[str]:
        """Return an absolute file path for ``url_path`` or None if not found."""
        if not url_path or url_path == "/":
            url_path = "/index.html"
        candidate = self._safe_join(url_path)
        if candidate is None:
            return None
        if os.path.isdir(candidate):
            index = os.path.join(candidate, "index.html")
            if os.path.isfile(index):
                return index
            candidate = None
        elif os.path.isfile(candidate):
            return candidate
        else:
            candidate = None
        if candidate is None and self.spa_fallback:
            index = os.path.join(self.public_dir, "index.html")
            if os.path.isfile(index):
                return index
        return None

    def serve(self, url_path: str) -> Optional[Tuple[bytes, str]]:
        """Return ``(content_bytes, content_type)`` for ``url_path`` or None."""
        path = self.resolve(url_path)
        if path is None:
            return None
        ctype, _ = mimetypes.guess_type(path)
        with open(path, "rb") as fh:
            return fh.read(), (ctype or "application/octet-stream")
