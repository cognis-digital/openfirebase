import os

import pytest

from openfirebase.storage import MemoryStore, SqliteStore, make_store


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        s = MemoryStore()
    else:
        s = SqliteStore(os.path.join(str(tmp_path), "t.sqlite3"))
    yield s
    s.close()


def test_set_get_roundtrip(store):
    store.set("ns", "k", {"a": 1, "b": [1, 2, 3]})
    assert store.get("ns", "k") == {"a": 1, "b": [1, 2, 3]}


def test_get_missing_returns_none(store):
    assert store.get("ns", "missing") is None


def test_overwrite(store):
    store.set("ns", "k", 1)
    store.set("ns", "k", 2)
    assert store.get("ns", "k") == 2


def test_delete(store):
    store.set("ns", "k", 1)
    assert store.delete("ns", "k") is True
    assert store.delete("ns", "k") is False
    assert store.get("ns", "k") is None


def test_items_and_isolation(store):
    store.set("a", "x", 1)
    store.set("a", "y", 2)
    store.set("b", "z", 3)
    a_items = dict(store.items("a"))
    assert a_items == {"x": 1, "y": 2}
    assert dict(store.items("b")) == {"z": 3}


def test_clear_namespace(store):
    store.set("a", "x", 1)
    store.set("b", "z", 3)
    store.clear("a")
    assert dict(store.items("a")) == {}
    assert dict(store.items("b")) == {"z": 3}


def test_memory_get_returns_copy():
    s = MemoryStore()
    s.set("ns", "k", {"a": 1})
    got = s.get("ns", "k")
    got["a"] = 999
    assert s.get("ns", "k") == {"a": 1}


def test_sqlite_persists_across_instances(tmp_path):
    path = os.path.join(str(tmp_path), "p.sqlite3")
    s1 = SqliteStore(path)
    s1.set("ns", "k", {"hello": "world"})
    s1.close()
    s2 = SqliteStore(path)
    assert s2.get("ns", "k") == {"hello": "world"}
    s2.close()


def test_make_store_memory_vs_sqlite(tmp_path):
    assert isinstance(make_store(None), MemoryStore)
    s = make_store(str(tmp_path))
    assert isinstance(s, SqliteStore)
    s.close()
