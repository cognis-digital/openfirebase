"""Unit tests for the deep Hosting features added in the messaging+compute pass.

Covers:
* Redirects (source pattern → destination + status)
* Rewrites (path destination + function name)
* Custom headers per glob pattern
* Preview channels (create / list / delete / overlay resolution)
* glob pattern matching (*, **)
* serve_with_headers three-tuple API
"""

import os

import pytest

from openfirebase.hosting import Hosting, _pattern_matches


# ---- Fixtures -----------------------------------------------------------

@pytest.fixture
def public(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    (root / "index.html").write_text("<h1>main</h1>", encoding="utf-8")
    (root / "about.html").write_text("<h1>about</h1>", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
    return str(root)


@pytest.fixture
def hosting(public):
    return Hosting(
        public,
        spa_fallback=False,
        rewrites=[
            {"source": "/api/**", "function": "myApi"},
            {"source": "/legacy", "destination": "/about.html"},
        ],
        redirects=[
            {"source": "/old-about", "destination": "/about.html", "type": 301},
            {"source": "/temp-redirect", "destination": "/index.html", "type": 302},
        ],
        headers=[
            {"source": "**/*.html", "headers": [
                {"key": "X-Frame-Options", "value": "DENY"},
                {"key": "X-Content-Type", "value": "nosniff"},
            ]},
            {"source": "/assets/**", "headers": [
                {"key": "Cache-Control", "value": "max-age=31536000"},
            ]},
        ],
    )


# ---- Pattern matching ---------------------------------------------------

def test_glob_exact_match():
    assert _pattern_matches("/old-about", "/old-about")
    assert not _pattern_matches("/old-about", "/new-about")


def test_glob_single_star():
    assert _pattern_matches("/items/*", "/items/abc")
    assert not _pattern_matches("/items/*", "/items/a/b")


def test_glob_double_star():
    assert _pattern_matches("/api/**", "/api/users/list")
    assert _pattern_matches("/api/**", "/api/")
    assert _pattern_matches("**/*.html", "/deep/path/index.html")


def test_glob_extension():
    assert _pattern_matches("**/*.js", "/assets/app.js")
    assert not _pattern_matches("**/*.js", "/assets/app.css")


# ---- Redirects ----------------------------------------------------------

def test_check_redirect_matches(hosting):
    redir = hosting.check_redirect("/old-about")
    assert redir is not None
    assert redir["destination"] == "/about.html"
    assert redir["status"] == 301


def test_check_redirect_302(hosting):
    redir = hosting.check_redirect("/temp-redirect")
    assert redir["status"] == 302


def test_check_redirect_no_match(hosting):
    assert hosting.check_redirect("/index.html") is None


def test_check_redirect_default_status(public):
    h = Hosting(public, redirects=[{"source": "/a", "destination": "/b"}])
    redir = h.check_redirect("/a")
    assert redir["status"] == 301


# ---- Rewrites -----------------------------------------------------------

def test_check_rewrite_function(hosting):
    result = hosting.check_rewrite("/api/users")
    assert result is not None
    assert result.get("function") == "myApi"


def test_check_rewrite_path(hosting):
    result = hosting.check_rewrite("/legacy")
    assert result is not None
    assert result.get("rewritten_path") == "/about.html"


def test_check_rewrite_no_match(hosting):
    assert hosting.check_rewrite("/index.html") is None


def test_rewrite_path_resolution(hosting):
    """A path-rewrite destination should be resolved when serving."""
    data, ctype = hosting.serve("/legacy")
    assert data is not None
    assert b"about" in data


# ---- Custom headers -----------------------------------------------------

def test_get_headers_html(hosting):
    headers = hosting.get_extra_headers("/index.html")
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("X-Content-Type") == "nosniff"


def test_get_headers_assets(hosting):
    headers = hosting.get_extra_headers("/assets/app.js")
    assert headers.get("Cache-Control") == "max-age=31536000"


def test_get_headers_no_match(hosting):
    headers = hosting.get_extra_headers("/data.json")
    assert headers == {}


def test_headers_later_rule_wins(public):
    """Later rules should overwrite earlier rules on the same key."""
    h = Hosting(
        public,
        headers=[
            {"source": "/**", "headers": [{"key": "X-Test", "value": "first"}]},
            {"source": "/index.html", "headers": [{"key": "X-Test", "value": "second"}]},
        ],
    )
    assert h.get_extra_headers("/index.html")["X-Test"] == "second"


def test_headers_merged_from_multiple_rules(public):
    h = Hosting(
        public,
        headers=[
            {"source": "/**", "headers": [{"key": "A", "value": "1"}]},
            {"source": "/index.html", "headers": [{"key": "B", "value": "2"}]},
        ],
    )
    headers = h.get_extra_headers("/index.html")
    assert headers["A"] == "1"
    assert headers["B"] == "2"


# ---- serve_with_headers -------------------------------------------------

def test_serve_with_headers_returns_three_tuple(hosting):
    result = hosting.serve_with_headers("/index.html")
    assert result is not None
    assert len(result) == 3
    data, ctype, headers = result
    assert b"main" in data
    assert "X-Frame-Options" in headers


def test_serve_with_headers_missing_returns_none(hosting):
    assert hosting.serve_with_headers("/nope.html") is None


# ---- Preview channels ---------------------------------------------------

def test_create_channel(hosting):
    name = hosting.create_channel("beta")
    assert name == "beta"
    channels = hosting.list_channels()
    assert any(c["name"] == "beta" for c in channels)


def test_delete_channel(hosting):
    hosting.create_channel("canary")
    ok = hosting.delete_channel("canary")
    assert ok is True
    assert not any(c["name"] == "canary" for c in hosting.list_channels())


def test_delete_nonexistent_channel(hosting):
    assert hosting.delete_channel("ghost") is False


def test_get_channel_url(hosting):
    hosting.create_channel("preview-1")
    url = hosting.get_channel_url("preview-1", "http://localhost:9090")
    assert "preview-1" in url
    assert url.startswith("http://localhost:9090")


def test_get_channel_url_missing_raises(hosting):
    with pytest.raises(KeyError):
        hosting.get_channel_url("no-such-channel")


def test_channel_overlay_shadows_main(public, tmp_path):
    """Files in the overlay dir should be served instead of the main dir."""
    overlay = tmp_path / "channel_overlay"
    overlay.mkdir()
    (overlay / "index.html").write_text("<h1>channel</h1>", encoding="utf-8")
    h = Hosting(public)
    h.create_channel("beta", str(overlay))
    data, _ = h.serve("/", channel="beta")
    assert b"channel" in data


def test_channel_falls_through_to_main(public, tmp_path):
    """Files not in the overlay dir should fall through to the main dir."""
    overlay = tmp_path / "channel_overlay2"
    overlay.mkdir()
    # about.html is NOT in the overlay
    h = Hosting(public)
    h.create_channel("beta2", str(overlay))
    data, _ = h.serve("/about.html", channel="beta2")
    assert b"about" in data


def test_channel_root(hosting):
    hosting.create_channel("test", "/some/path")
    root = hosting.channel_root("test")
    assert root == os.path.abspath("/some/path")


def test_channel_root_missing(hosting):
    assert hosting.channel_root("nope") is None


# ---- SPA fallback with rewrites ----------------------------------------

def test_spa_fallback_after_rewrite(public):
    h = Hosting(
        public,
        spa_fallback=True,
        rewrites=[{"source": "/app/**", "destination": "/index.html"}],
    )
    data, _ = h.serve("/app/dashboard")
    assert b"main" in data


# ---- Backward-compat two-tuple API --------------------------------------

def test_serve_still_returns_two_tuple(hosting):
    result = hosting.serve("/index.html")
    assert result is not None
    assert len(result) == 2
    data, ctype = result
    assert b"main" in data
