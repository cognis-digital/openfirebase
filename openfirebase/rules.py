"""Security Rules engine — parse and evaluate a meaningful subset of
Firestore/Storage rules DSL.

Supported subset
----------------
* ``service cloud.firestore`` / ``service firebase.storage`` declarations
* ``match /<path>/{wildcard}`` — static and wildcard path segments, including
  ``{wildcard=**}`` double-wildcard (recursive)
* ``allow read, write: if <expr>``  (separate ``read``/``write``/``get``/``list``/
  ``create``/``update``/``delete``)
* Condition expressions:
    - boolean literals  ``true`` / ``false``
    - ``request.auth != null``  /  ``request.auth == null``
    - ``request.auth.uid == "<literal>"``
    - ``request.auth.uid == resource.data.<field>``
    - ``request.auth.token.<claim> == "<literal>"``
    - ``resource.data.<field> == <value>``
    - ``request.resource.data.<field> == <value>``
    - ``request.resource.data.<field> is <type>``   (string / number / bool / map / list)
    - ``<expr> && <expr>``  /  ``<expr> || <expr>``
    - ``!<expr>``
    - parenthesised subexpressions ``(<expr>)``

This is a LOCAL development helper.  It does NOT faithfully replicate every
nuance of the real rules language and must NOT be used to secure real systems.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------

class RulesError(Exception):
    """Raised when rules cannot be parsed or evaluated."""


class PermissionDenied(Exception):
    """Raised when a security rule denies the requested operation."""


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r'(?P<COMMENT>//[^\n]*)'
    r'|(?P<SPACE>\s+)'
    r'|(?P<STRING>"[^"]*"|\'[^\']*\')'
    r'|(?P<ARROW>=>)'
    r'|(?P<EQEQ>==)'
    r'|(?P<NEQ>!=)'
    r'|(?P<AND>&&)'
    r'|(?P<OR>\|\|)'
    r'|(?P<BANG>!(?!=))'
    r'|(?P<LBRACE>\{)'
    r'|(?P<RBRACE>\})'
    r'|(?P<LPAREN>\()'
    r'|(?P<RPAREN>\))'
    r'|(?P<LBRACKET>\[)'
    r'|(?P<RBRACKET>\])'
    r'|(?P<COMMA>,)'
    r'|(?P<SEMI>;)'
    r'|(?P<COLON>:)'
    r'|(?P<SLASH>/)'
    r'|(?P<DOT>\.)'
    r'|(?P<STAR>\*)'
    r'|(?P<EQ>=(?!=))'
    r'|(?P<WORD>[A-Za-z_\$][A-Za-z0-9_\$]*)'
    r'|(?P<NUMBER>-?\d+(?:\.\d+)?)'
    r'|(?P<OTHER>\S)'
)


def _tokenise(src: str) -> List[Tuple[str, str]]:
    tokens = []
    for m in _TOKEN_RE.finditer(src):
        kind = m.lastgroup
        if kind in ("SPACE", "COMMENT"):
            continue
        tokens.append((kind, m.group()))
    return tokens


# ---------------------------------------------------------------------------
# Simple recursive-descent parser
# ---------------------------------------------------------------------------

class _Parser:
    """Parses a rules source string into an internal AST (plain dicts)."""

    def __init__(self, tokens: List[Tuple[str, str]]) -> None:
        self._tok = tokens
        self._pos = 0

    # ---- look-ahead / consume helpers ------------------------------------

    def _peek(self, offset: int = 0) -> Optional[Tuple[str, str]]:
        idx = self._pos + offset
        return self._tok[idx] if idx < len(self._tok) else None

    def _peek_val(self, offset: int = 0) -> str:
        t = self._peek(offset)
        return t[1] if t else ""

    def _consume(self) -> Tuple[str, str]:
        t = self._tok[self._pos]
        self._pos += 1
        return t

    def _expect(self, kind: str, val: Optional[str] = None) -> str:
        t = self._consume()
        if t[0] != kind:
            raise RulesError(
                f"expected {kind!r} but got {t[0]!r} ({t[1]!r})")
        if val is not None and t[1] != val:
            raise RulesError(
                f"expected {val!r} but got {t[1]!r}")
        return t[1]

    def _match_word(self, *words) -> bool:
        t = self._peek()
        return t is not None and t[0] == "WORD" and t[1] in words

    def _skip_word(self, *words) -> bool:
        if self._match_word(*words):
            self._consume()
            return True
        return False

    # ---- top-level -------------------------------------------------------

    def parse(self):
        """Parse the whole rules file.  Returns a list of service blocks."""
        services = []
        while self._pos < len(self._tok):
            if self._match_word("rules_version"):
                self._parse_rules_version()
            elif self._match_word("service"):
                services.append(self._parse_service())
            else:
                self._consume()   # skip unknown tokens
        return services

    def _parse_rules_version(self):
        self._consume()   # "rules_version"
        self._expect("EQ")
        self._consume()   # version string/number
        self._expect("SEMI")

    def _parse_service(self):
        self._expect("WORD", "service")
        # service name may span multiple tokens e.g. "cloud.firestore"
        parts = [self._expect("WORD")]
        while self._peek() and self._peek()[0] == "DOT":
            self._consume()  # dot
            parts.append(self._expect("WORD"))
        service_name = ".".join(parts)
        self._expect("LBRACE")
        matches = []
        while not (self._peek() and self._peek()[0] == "RBRACE"):
            if self._peek() is None:
                break
            if self._match_word("match"):
                matches.append(self._parse_match())
            else:
                self._consume()
        self._expect("RBRACE")
        return {"service": service_name, "matches": matches}

    def _parse_match(self):
        self._expect("WORD", "match")
        path_segments = self._parse_match_path()
        self._expect("LBRACE")
        rules = []
        nested = []
        while not (self._peek() and self._peek()[0] == "RBRACE"):
            if self._peek() is None:
                break
            if self._match_word("allow"):
                rules.append(self._parse_allow())
            elif self._match_word("match"):
                nested.append(self._parse_match())
            else:
                self._consume()
        self._expect("RBRACE")
        return {"path": path_segments, "rules": rules, "nested": nested}

    def _parse_match_path(self) -> List[str]:
        """Parse slash-delimited path like /users/{uid} → ['users', '{uid}'].

        Stops before the block-opening LBRACE that follows the path.
        A LBRACE is only treated as a wildcard opener when it appears after
        a SLASH (i.e., at the start of a new path segment).
        """
        segments: List[str] = []
        # Track whether we are "expecting a segment" (just after a slash or at start)
        after_slash = True
        while True:
            t = self._peek()
            if t is None:
                break
            kind = t[0]
            if kind == "SLASH":
                self._consume()
                after_slash = True
            elif kind == "WORD" and after_slash:
                segments.append(self._consume()[1])
                after_slash = False
            elif kind == "LBRACE" and after_slash:
                # wildcard segment
                self._consume()  # {
                wildcard = self._expect("WORD")
                # optional =**
                if self._peek() and self._peek()[0] == "EQ":
                    self._consume()  # =
                    self._expect("STAR")
                    self._expect("STAR")
                    wildcard = "{" + wildcard + "=**}"
                else:
                    wildcard = "{" + wildcard + "}"
                self._expect("RBRACE")
                segments.append(wildcard)
                after_slash = False
            elif kind == "STAR" and after_slash:
                self._consume()
                if self._peek() and self._peek()[0] == "STAR":
                    self._consume()
                    segments.append("{=**}")
                else:
                    segments.append("{=*}")
                after_slash = False
            else:
                # Anything else (including a LBRACE not after slash = block open) stops us
                break
        return segments

    def _parse_allow(self) -> dict:
        self._expect("WORD", "allow")
        ops = self._parse_allow_ops()
        self._expect("COLON")
        self._expect("WORD", "if")
        expr = self._parse_expr()
        # optional semicolon
        if self._peek() and self._peek()[0] == "SEMI":
            self._consume()
        return {"ops": ops, "expr": expr}

    def _parse_allow_ops(self) -> List[str]:
        ops = []
        while True:
            t = self._peek()
            if t is None or t[0] != "WORD":
                break
            val = t[1]
            if val in ("read", "write", "get", "list", "create",
                       "update", "delete", "if"):
                if val == "if":
                    break
                self._consume()
                ops.append(val)
                if self._peek() and self._peek()[0] == "COMMA":
                    self._consume()
            else:
                break
        return ops

    # ---- expression parser  (precedence: || > && > ! > atom) -------------

    def _parse_expr(self) -> dict:
        return self._parse_or()

    def _parse_or(self) -> dict:
        left = self._parse_and()
        while self._peek() and self._peek()[0] == "OR":
            self._consume()
            right = self._parse_and()
            left = {"op": "||", "left": left, "right": right}
        return left

    def _parse_and(self) -> dict:
        left = self._parse_not()
        while self._peek() and self._peek()[0] == "AND":
            self._consume()
            right = self._parse_not()
            left = {"op": "&&", "left": left, "right": right}
        return left

    def _parse_not(self) -> dict:
        if self._peek() and self._peek()[0] == "BANG":
            self._consume()
            operand = self._parse_not()
            return {"op": "!", "operand": operand}
        return self._parse_comparison()

    def _parse_comparison(self) -> dict:
        left = self._parse_atom()
        t = self._peek()
        if t and t[0] in ("EQEQ", "NEQ"):
            op = self._consume()[1]
            right = self._parse_atom()
            return {"op": op, "left": left, "right": right}
        if t and t[0] == "WORD" and t[1] == "is":
            self._consume()
            type_name = self._expect("WORD")
            return {"op": "is", "left": left, "type": type_name}
        return left

    def _parse_atom(self) -> dict:
        t = self._peek()
        if t is None:
            return {"op": "literal", "value": None}

        # parenthesised expression
        if t[0] == "LPAREN":
            self._consume()
            expr = self._parse_expr()
            self._expect("RPAREN")
            return expr

        # string literal
        if t[0] == "STRING":
            self._consume()
            s = t[1][1:-1]   # strip quotes
            return {"op": "literal", "value": s}

        # number literal
        if t[0] == "NUMBER":
            self._consume()
            v = float(t[1]) if "." in t[1] else int(t[1])
            return {"op": "literal", "value": v}

        # boolean / null literals, or dot-path references
        if t[0] == "WORD":
            val = t[1]
            if val == "true":
                self._consume()
                return {"op": "literal", "value": True}
            if val == "false":
                self._consume()
                return {"op": "literal", "value": False}
            if val == "null":
                self._consume()
                return {"op": "literal", "value": None}
            # dot-access chain: request.auth.uid  resource.data.field
            return self._parse_dotpath()

        self._consume()
        return {"op": "literal", "value": None}

    def _parse_dotpath(self) -> dict:
        """Parse a dot-separated path like request.auth.uid."""
        parts = [self._expect("WORD")]
        while self._peek() and self._peek()[0] == "DOT":
            self._consume()
            parts.append(self._expect("WORD"))
        # handle bracket subscript: resource.data["field"]
        if self._peek() and self._peek()[0] == "LBRACKET":
            self._consume()
            key_tok = self._consume()
            key = key_tok[1][1:-1] if key_tok[0] == "STRING" else key_tok[1]
            self._expect("RBRACKET")
            parts.append(key)
        return {"op": "ref", "path": parts}


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

# Expand compound ops to atomic ones
_OP_MAP = {
    "read": ["get", "list"],
    "write": ["create", "update", "delete"],
}


def _expand_ops(ops: List[str]) -> List[str]:
    expanded = []
    for op in ops:
        expanded.extend(_OP_MAP.get(op, [op]))
    return expanded


def _match_path(rule_segments: List[str],
                resource_parts: List[str]) -> Optional[Dict[str, str]]:
    """Match rule path segments against actual resource path parts.

    Returns a dict of wildcard bindings if matched, or None.
    Double-wildcards ({x=**}) consume zero or more segments.
    """
    wildcards: Dict[str, str] = {}
    ri = 0
    for si, seg in enumerate(rule_segments):
        if seg.startswith("{") and seg.endswith("}"):
            inner = seg[1:-1]
            if inner.endswith("=**"):
                name = inner[:-3]
                # consume all remaining segments
                remaining = resource_parts[ri:]
                wildcards[name] = "/".join(remaining)
                ri = len(resource_parts)
                # must be last segment
                if si != len(rule_segments) - 1:
                    return None
                break
            else:
                if ri >= len(resource_parts):
                    return None
                wildcards[inner] = resource_parts[ri]
                ri += 1
        elif seg.startswith("{="):
            if ri >= len(resource_parts):
                return None
            ri += 1
        else:
            if ri >= len(resource_parts) or resource_parts[ri] != seg:
                return None
            ri += 1
    if ri != len(resource_parts):
        return None
    return wildcards


def _resolve_ref(path_parts: List[str], ctx: Dict[str, Any]) -> Any:
    """Walk a dotted reference path through the evaluation context."""
    node = ctx
    for part in path_parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _eval_expr(expr: dict, ctx: Dict[str, Any]) -> Any:
    op = expr["op"]

    if op == "literal":
        return expr["value"]

    if op == "ref":
        return _resolve_ref(expr["path"], ctx)

    if op == "!":
        return not _eval_expr(expr["operand"], ctx)

    if op == "&&":
        return bool(_eval_expr(expr["left"], ctx)) and \
               bool(_eval_expr(expr["right"], ctx))

    if op == "||":
        return bool(_eval_expr(expr["left"], ctx)) or \
               bool(_eval_expr(expr["right"], ctx))

    if op == "==":
        return _eval_expr(expr["left"], ctx) == _eval_expr(expr["right"], ctx)

    if op == "!=":
        return _eval_expr(expr["left"], ctx) != _eval_expr(expr["right"], ctx)

    if op == "is":
        val = _eval_expr(expr["left"], ctx)
        type_name = expr["type"]
        type_map = {
            "string": str,
            "number": (int, float),
            "bool": bool,
            "map": dict,
            "list": list,
        }
        expected = type_map.get(type_name)
        if expected is None:
            return False
        return isinstance(val, expected)

    return None


# ---------------------------------------------------------------------------
# RulesEngine — the public interface
# ---------------------------------------------------------------------------

_READ_OPS = frozenset(["get", "list"])
_WRITE_OPS = frozenset(["create", "update", "delete"])


class RulesEngine:
    """Parse and evaluate security rules for Firestore or Storage paths.

    Usage::

        engine = RulesEngine()
        engine.load_rules('''
            service cloud.firestore {
              match /databases/{db}/documents {
                match /users/{uid} {
                  allow read, write: if request.auth != null
                      && request.auth.uid == uid;
                }
              }
            }
        ''')

        # Build evaluation context from auth token payload (or None)
        ctx = engine.make_context(auth_payload={"sub": "u123"})
        engine.check("cloud.firestore", "/users/u123", "read", ctx)
        # raises PermissionDenied if denied
    """

    def __init__(self) -> None:
        self._services: List[dict] = []

    def load_rules(self, src: str) -> None:
        """Parse and replace the currently loaded rules."""
        tokens = _tokenise(src)
        parser = _Parser(tokens)
        self._services = parser.parse()

    @staticmethod
    def make_context(auth_payload: Optional[Dict[str, Any]] = None,
                     resource_data: Optional[Dict[str, Any]] = None,
                     request_resource_data: Optional[Dict[str, Any]] = None,
                     ) -> Dict[str, Any]:
        """Build an evaluation context dict.

        Parameters
        ----------
        auth_payload:
            The verified JWT payload dict.  ``sub`` becomes ``uid``; the full
            payload is available as ``request.auth.token``.
        resource_data:
            The current document/object data (before the write).
        request_resource_data:
            The incoming document data (for create/update).
        """
        if auth_payload:
            uid = auth_payload.get("sub") or auth_payload.get("uid", "")
            auth_obj: Any = {
                "uid": uid,
                "token": dict(auth_payload),
            }
        else:
            auth_obj = None

        return {
            "request": {
                "auth": auth_obj,
                "resource": {
                    "data": request_resource_data or {},
                },
            },
            "resource": {
                "data": resource_data or {},
            },
        }

    def check(self, service: str, resource_path: str,
              operation: str, ctx: Dict[str, Any]) -> None:
        """Evaluate rules for *operation* on *resource_path*.

        Raises :class:`PermissionDenied` if no ``allow`` rule permits the
        operation.  Silently returns if permitted.

        Parameters
        ----------
        service:
            ``"cloud.firestore"`` or ``"firebase.storage"``.
        resource_path:
            Absolute path, e.g. ``"/users/u123"`` or
            ``"/databases/default/documents/users/u123"``.
        operation:
            One of ``get``, ``list``, ``create``, ``update``, ``delete``.
        ctx:
            An evaluation context built with :meth:`make_context`.
        """
        # Normalise atomic ops
        op = operation.lower()
        if op not in ("get", "list", "create", "update", "delete",
                      "read", "write"):
            raise RulesError(f"unknown operation: {op!r}")
        # Expand compound ops for matching
        atomic_ops = _expand_ops([op])

        parts = [p for p in resource_path.split("/") if p]

        for svc in self._services:
            if svc["service"] != service:
                continue
            for allowed in _eval_service_matches(
                    svc["matches"], parts, atomic_ops, ctx, []):
                if allowed:
                    return
                # An explicit False means a deny rule was matched; keep looking
                # (Firebase rules: first allow wins, denies are implicit)

        raise PermissionDenied(
            f"operation {op!r} on {resource_path!r} is not permitted")

    def is_allowed(self, service: str, resource_path: str,
                   operation: str, ctx: Dict[str, Any]) -> bool:
        """Like :meth:`check` but returns bool instead of raising."""
        try:
            self.check(service, resource_path, operation, ctx)
            return True
        except PermissionDenied:
            return False


def _eval_service_matches(matches: List[dict], parts: List[str],
                           ops: List[str], ctx: Dict[str, Any],
                           parent_wildcards: List[Dict[str, str]]):
    """Yield True for each allow rule that matches + permits."""
    for match_block in matches:
        rule_segs = match_block["path"]
        # determine which parts this match consumes
        # double-wildcard blocks are greedy — try consuming prefix
        has_dw = any(
            s.startswith("{") and s.endswith("}") and s[1:-1].endswith("=**")
            for s in rule_segs
        )
        if has_dw:
            # Try all possible prefix lengths
            for prefix_len in range(len(rule_segs) - 1, len(parts) + 1):
                prefix = parts[:prefix_len]
                tail = parts[prefix_len:]
                wc = _match_path(rule_segs, prefix)
                if wc is None:
                    continue
                merged_wc = {**{k: v for d in parent_wildcards for k, v in d.items()},
                             **wc}
                local_ctx = _inject_wildcards(ctx, merged_wc)
                for rule in match_block["rules"]:
                    if _op_matches(rule["ops"], ops):
                        val = _eval_expr(rule["expr"], local_ctx)
                        yield bool(val)
                # recurse nested matches with remaining parts
                if tail and match_block["nested"]:
                    yield from _eval_service_matches(
                        match_block["nested"], tail, ops, local_ctx,
                        [merged_wc])
        else:
            # Static prefix — rule_segs must match a prefix of parts
            wc = _match_path(rule_segs, parts[:len(rule_segs)])
            if wc is None:
                continue
            merged_wc = {**{k: v for d in parent_wildcards for k, v in d.items()},
                         **wc}
            local_ctx = _inject_wildcards(ctx, merged_wc)
            remaining = parts[len(rule_segs):]
            for rule in match_block["rules"]:
                if _op_matches(rule["ops"], ops) and not remaining:
                    val = _eval_expr(rule["expr"], local_ctx)
                    yield bool(val)
            # recurse
            if remaining and match_block["nested"]:
                yield from _eval_service_matches(
                    match_block["nested"], remaining, ops, local_ctx,
                    [merged_wc])


def _inject_wildcards(ctx: Dict[str, Any],
                      wildcards: Dict[str, str]) -> Dict[str, Any]:
    """Return a shallow copy of ctx with wildcards injected at top level."""
    merged = dict(ctx)
    merged.update(wildcards)
    return merged


def _op_matches(rule_ops: List[str], target_ops: List[str]) -> bool:
    expanded = set(_expand_ops(rule_ops))
    return bool(expanded.intersection(target_ops))
