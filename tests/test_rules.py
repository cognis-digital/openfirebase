"""Unit tests for the Security Rules engine (openfirebase.rules)."""

import pytest

from openfirebase.rules import (
    RulesEngine,
    RulesError,
    PermissionDenied,
    _tokenise,
    _Parser,
    _match_path,
)


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

class TestTokeniser:
    def test_basic_tokens(self):
        src = "service cloud.firestore { }"
        toks = _tokenise(src)
        kinds = [k for k, _ in toks]
        assert "WORD" in kinds
        assert "DOT" in kinds
        assert "LBRACE" in kinds
        assert "RBRACE" in kinds

    def test_comment_stripped(self):
        src = "// a comment\nservice foo { }"
        toks = _tokenise(src)
        vals = [v for _, v in toks]
        assert "// a comment" not in vals

    def test_string_literal(self):
        toks = _tokenise('"hello"')
        assert toks[0] == ("STRING", '"hello"')

    def test_operators(self):
        toks = _tokenise("== != && ||")
        kinds = [k for k, _ in toks]
        assert kinds == ["EQEQ", "NEQ", "AND", "OR"]


# ---------------------------------------------------------------------------
# Path matching
# ---------------------------------------------------------------------------

class TestMatchPath:
    def test_static_exact(self):
        assert _match_path(["users", "u1"], ["users", "u1"]) == {}

    def test_static_mismatch(self):
        assert _match_path(["users", "u1"], ["users", "u2"]) is None

    def test_wildcard_single(self):
        wc = _match_path(["users", "{uid}"], ["users", "abc123"])
        assert wc == {"uid": "abc123"}

    def test_wildcard_double(self):
        wc = _match_path(["databases", "{db}", "documents", "{path=**}"],
                         ["databases", "default", "documents", "users", "u1"])
        assert wc is not None
        assert wc["db"] == "default"
        assert "users/u1" in wc["path"]

    def test_too_few_parts(self):
        assert _match_path(["users", "{uid}"], ["users"]) is None

    def test_too_many_parts(self):
        assert _match_path(["users", "{uid}"], ["users", "u1", "extra"]) is None


# ---------------------------------------------------------------------------
# Parser smoke tests
# ---------------------------------------------------------------------------

class TestParser:
    def _parse(self, src):
        return _Parser(_tokenise(src)).parse()

    def test_empty(self):
        assert self._parse("") == []

    def test_service_block(self):
        src = "service cloud.firestore { }"
        result = self._parse(src)
        assert len(result) == 1
        assert result[0]["service"] == "cloud.firestore"

    def test_allow_true(self):
        src = """
        service cloud.firestore {
            match /users/{uid} {
                allow read, write: if true;
            }
        }
        """
        result = self._parse(src)
        rules = result[0]["matches"][0]["rules"]
        assert any("read" in r["ops"] for r in rules)

    def test_allow_false(self):
        src = """
        service cloud.firestore {
            match /private/{doc} {
                allow read: if false;
            }
        }
        """
        result = self._parse(src)
        rule = result[0]["matches"][0]["rules"][0]
        assert rule["expr"]["value"] is False

    def test_rules_version_skipped(self):
        src = """
        rules_version = '2';
        service cloud.firestore {
            match /x/{id} {
                allow read: if true;
            }
        }
        """
        result = self._parse(src)
        assert len(result) == 1

    def test_nested_match(self):
        src = """
        service cloud.firestore {
            match /users/{uid} {
                allow read: if true;
                match /posts/{postId} {
                    allow write: if true;
                }
            }
        }
        """
        result = self._parse(src)
        outer = result[0]["matches"][0]
        assert len(outer["nested"]) == 1
        assert outer["nested"][0]["path"][0] == "posts"


# ---------------------------------------------------------------------------
# RulesEngine — full evaluation
# ---------------------------------------------------------------------------

_BASIC_RULES = """
service cloud.firestore {
    match /users/{uid} {
        allow read: if true;
        allow write: if request.auth != null && request.auth.uid == uid;
    }
    match /private/{doc} {
        allow read, write: if false;
    }
}
"""

_AUTH_CLAIM_RULES = """
service cloud.firestore {
    match /admin/{doc} {
        allow read, write: if request.auth != null
            && request.auth.token.role == "admin";
    }
}
"""

_RESOURCE_RULES = """
service cloud.firestore {
    match /posts/{postId} {
        allow update: if request.auth.uid == resource.data.owner;
    }
}
"""

_TYPE_CHECK_RULES = """
service cloud.firestore {
    match /items/{id} {
        allow create: if request.resource.data.name is string
            && request.resource.data.count is number;
    }
}
"""

_STORAGE_RULES = """
service firebase.storage {
    match /images/{name} {
        allow read: if true;
        allow write: if request.auth != null;
    }
}
"""


class TestRulesEngine:
    def setup_method(self):
        self.engine = RulesEngine()

    def test_allow_read_unauthenticated(self):
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context(auth_payload=None)
        # read is allowed for everyone
        self.engine.check("cloud.firestore", "/users/u1", "get", ctx)

    def test_allow_write_authenticated_matching_uid(self):
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context(auth_payload={"sub": "u1"})
        self.engine.check("cloud.firestore", "/users/u1", "create", ctx)

    def test_deny_write_unauthenticated(self):
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context(auth_payload=None)
        with pytest.raises(PermissionDenied):
            self.engine.check("cloud.firestore", "/users/u1", "create", ctx)

    def test_deny_write_wrong_uid(self):
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context(auth_payload={"sub": "other"})
        with pytest.raises(PermissionDenied):
            self.engine.check("cloud.firestore", "/users/u1", "update", ctx)

    def test_private_always_denied_read(self):
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context(auth_payload={"sub": "u1"})
        with pytest.raises(PermissionDenied):
            self.engine.check("cloud.firestore", "/private/doc1", "get", ctx)

    def test_private_always_denied_write(self):
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context(auth_payload={"sub": "u1"})
        with pytest.raises(PermissionDenied):
            self.engine.check("cloud.firestore", "/private/doc1", "delete", ctx)

    def test_auth_token_claim(self):
        self.engine.load_rules(_AUTH_CLAIM_RULES)
        ctx = self.engine.make_context(
            auth_payload={"sub": "u1", "role": "admin"})
        self.engine.check("cloud.firestore", "/admin/settings", "get", ctx)

    def test_auth_token_claim_denied(self):
        self.engine.load_rules(_AUTH_CLAIM_RULES)
        ctx = self.engine.make_context(
            auth_payload={"sub": "u1", "role": "viewer"})
        with pytest.raises(PermissionDenied):
            self.engine.check("cloud.firestore", "/admin/settings", "get", ctx)

    def test_resource_data_comparison(self):
        self.engine.load_rules(_RESOURCE_RULES)
        ctx = self.engine.make_context(
            auth_payload={"sub": "alice"},
            resource_data={"owner": "alice"},
        )
        self.engine.check("cloud.firestore", "/posts/p1", "update", ctx)

    def test_resource_data_comparison_denied(self):
        self.engine.load_rules(_RESOURCE_RULES)
        ctx = self.engine.make_context(
            auth_payload={"sub": "bob"},
            resource_data={"owner": "alice"},
        )
        with pytest.raises(PermissionDenied):
            self.engine.check("cloud.firestore", "/posts/p1", "update", ctx)

    def test_type_check_allowed(self):
        self.engine.load_rules(_TYPE_CHECK_RULES)
        ctx = self.engine.make_context(
            request_resource_data={"name": "widget", "count": 5},
        )
        self.engine.check("cloud.firestore", "/items/i1", "create", ctx)

    def test_type_check_denied_wrong_type(self):
        self.engine.load_rules(_TYPE_CHECK_RULES)
        ctx = self.engine.make_context(
            request_resource_data={"name": 123, "count": 5},
        )
        with pytest.raises(PermissionDenied):
            self.engine.check("cloud.firestore", "/items/i1", "create", ctx)

    def test_storage_service(self):
        self.engine.load_rules(_STORAGE_RULES)
        ctx = self.engine.make_context(auth_payload=None)
        self.engine.check("firebase.storage", "/images/logo.png", "get", ctx)

    def test_storage_write_denied_unauthenticated(self):
        self.engine.load_rules(_STORAGE_RULES)
        ctx = self.engine.make_context(auth_payload=None)
        with pytest.raises(PermissionDenied):
            self.engine.check("firebase.storage", "/images/logo.png", "create", ctx)

    def test_is_allowed_returns_bool(self):
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context(auth_payload=None)
        assert self.engine.is_allowed("cloud.firestore", "/users/u1", "get", ctx) is True
        assert self.engine.is_allowed("cloud.firestore", "/users/u1", "create", ctx) is False

    def test_unknown_service_denied(self):
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context()
        with pytest.raises(PermissionDenied):
            self.engine.check("firebase.unknown", "/users/u1", "get", ctx)

    def test_make_context_auth_payload(self):
        ctx = RulesEngine.make_context(auth_payload={"sub": "u99"})
        assert ctx["request"]["auth"]["uid"] == "u99"

    def test_make_context_no_auth(self):
        ctx = RulesEngine.make_context()
        assert ctx["request"]["auth"] is None

    def test_load_rules_bad_syntax_no_crash(self):
        # Should raise RulesError on truly malformed input that the parser can't handle
        # Or silently skip unknown tokens — test that it at least doesn't crash on partial
        engine = RulesEngine()
        # Partial/empty rules should not raise
        engine.load_rules("// just a comment")

    def test_compound_read_op(self):
        """read expands to get + list."""
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context(auth_payload=None)
        # list (GET on collection) should also be permitted
        self.engine.check("cloud.firestore", "/users/u1", "list", ctx)

    def test_compound_write_op(self):
        """write expands to create + update + delete."""
        self.engine.load_rules(_BASIC_RULES)
        ctx = self.engine.make_context(auth_payload={"sub": "u1"})
        self.engine.check("cloud.firestore", "/users/u1", "delete", ctx)

    def test_negation_expr(self):
        src = """
        service cloud.firestore {
            match /items/{id} {
                allow read: if !(request.auth == null);
            }
        }
        """
        self.engine.load_rules(src)
        ctx = self.engine.make_context(auth_payload={"sub": "u1"})
        self.engine.check("cloud.firestore", "/items/i1", "get", ctx)

    def test_or_expr(self):
        src = """
        service cloud.firestore {
            match /docs/{id} {
                allow read: if request.auth != null || true;
            }
        }
        """
        self.engine.load_rules(src)
        ctx = self.engine.make_context(auth_payload=None)
        self.engine.check("cloud.firestore", "/docs/d1", "get", ctx)
