import pytest

from openfirebase.rtdb import RealtimeDatabase


@pytest.fixture
def db():
    return RealtimeDatabase()


def test_set_get_root(db):
    db.set("/", {"a": 1})
    assert db.get("/") == {"a": 1}


def test_set_get_nested(db):
    db.set("/users/u1/name", "Ada")
    assert db.get("/users/u1/name") == "Ada"
    assert db.get("/users/u1") == {"name": "Ada"}


def test_get_missing_returns_none(db):
    assert db.get("/nope/here") is None


def test_set_creates_intermediate_nodes(db):
    db.set("/a/b/c/d", 42)
    assert db.get("/a/b/c") == {"d": 42}


def test_update_shallow_merge(db):
    db.set("/u/1", {"name": "Bo", "age": 20})
    db.update("/u/1", {"age": 21, "city": "NYC"})
    assert db.get("/u/1") == {"name": "Bo", "age": 21, "city": "NYC"}


def test_update_on_missing_creates(db):
    db.update("/fresh", {"x": 1})
    assert db.get("/fresh") == {"x": 1}


def test_update_requires_dict(db):
    with pytest.raises(TypeError):
        db.update("/x", [1, 2])


def test_push_generates_keys(db):
    k1 = db.push("/messages", {"text": "hi"})
    k2 = db.push("/messages", {"text": "yo"})
    assert k1 != k2
    msgs = db.get("/messages")
    assert msgs[k1] == {"text": "hi"}
    assert msgs[k2] == {"text": "yo"}


def test_push_ids_sort_chronologically(db):
    keys = [db.push("/log", {"i": i}) for i in range(5)]
    assert keys == sorted(keys)


def test_delete(db):
    db.set("/a/b", 1)
    db.set("/a/c", 2)
    assert db.delete("/a/b") is True
    assert db.get("/a") == {"c": 2}
    assert db.delete("/a/b") is False


def test_delete_root(db):
    db.set("/x", 1)
    assert db.delete("/") is True
    assert db.get("/") == {}


def test_overwrite_replaces_subtree(db):
    db.set("/a", {"b": {"c": 1}})
    db.set("/a/b", "scalar")
    assert db.get("/a/b") == "scalar"
