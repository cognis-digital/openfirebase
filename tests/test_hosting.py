import os

import pytest

from openfirebase.hosting import Hosting


@pytest.fixture
def public(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    (root / "index.html").write_text("<h1>home</h1>", encoding="utf-8")
    (root / "app.js").write_text("console.log(1)", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "index.html").write_text("<h1>sub</h1>", encoding="utf-8")
    return str(root)


def test_root_serves_index(public):
    h = Hosting(public)
    data, ctype = h.serve("/")
    assert b"home" in data
    assert ctype.startswith("text/html")


def test_serve_file(public):
    h = Hosting(public)
    data, ctype = h.serve("/app.js")
    assert b"console.log" in data
    assert "javascript" in ctype or ctype.startswith("text/")


def test_directory_index(public):
    h = Hosting(public)
    data, _ = h.serve("/sub")
    assert b"sub" in data


def test_missing_returns_none(public):
    h = Hosting(public)
    assert h.serve("/nope.html") is None


def test_spa_fallback(public):
    h = Hosting(public, spa_fallback=True)
    data, _ = h.serve("/some/spa/route")
    assert b"home" in data


def test_no_spa_fallback(public):
    h = Hosting(public, spa_fallback=False)
    assert h.serve("/some/spa/route") is None


def test_path_traversal_blocked(public):
    h = Hosting(public)
    assert h.resolve("/../../etc/passwd") is None
    assert h.serve("/../secret") is None
