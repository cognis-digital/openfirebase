"""Tests for the deepened Realtime Database features:
- RTDBQuery: orderByChild / orderByKey / orderByValue / equalTo / limitToFirst /
  limitToLast / startAt / endAt
- transaction(): atomic read-modify-write
- OnDisconnect: set / remove / update / cancel / simulate_disconnect
"""

import pytest

from openfirebase.rtdb import RealtimeDatabase, OnDisconnect


@pytest.fixture
def db():
    return RealtimeDatabase()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed_messages(db):
    db.set("/msgs/a", {"score": 10, "text": "hi"})
    db.set("/msgs/b", {"score": 5,  "text": "hey"})
    db.set("/msgs/c", {"score": 20, "text": "yo"})
    db.set("/msgs/d", {"score": 5,  "text": "sup"})


# ---------------------------------------------------------------------------
# orderByChild
# ---------------------------------------------------------------------------

def test_order_by_child_returns_sorted(db):
    _seed_messages(db)
    results = db.query("/msgs").order_by_child("score").get()
    scores = [v["score"] for v in results.values()]
    assert scores == sorted(scores)


def test_order_by_child_equal_to(db):
    _seed_messages(db)
    results = db.query("/msgs").order_by_child("score").equal_to(5).get()
    assert all(v["score"] == 5 for v in results.values())
    assert set(results.keys()) == {"b", "d"}


def test_order_by_child_limit_to_first(db):
    _seed_messages(db)
    results = db.query("/msgs").order_by_child("score").limit_to_first(2).get()
    scores = [v["score"] for v in results.values()]
    assert all(s <= 10 for s in scores)
    assert len(results) == 2


def test_order_by_child_limit_to_last(db):
    _seed_messages(db)
    results = db.query("/msgs").order_by_child("score").limit_to_last(1).get()
    assert len(results) == 1
    assert list(results.values())[0]["score"] == 20


def test_order_by_child_start_at(db):
    _seed_messages(db)
    results = db.query("/msgs").order_by_child("score").start_at(10).get()
    assert all(v["score"] >= 10 for v in results.values())


def test_order_by_child_end_at(db):
    _seed_messages(db)
    results = db.query("/msgs").order_by_child("score").end_at(10).get()
    assert all(v["score"] <= 10 for v in results.values())


def test_order_by_child_range(db):
    _seed_messages(db)
    results = db.query("/msgs").order_by_child("score").start_at(5).end_at(10).get()
    assert all(5 <= v["score"] <= 10 for v in results.values())


# ---------------------------------------------------------------------------
# orderByKey
# ---------------------------------------------------------------------------

def test_order_by_key_sorts_lexicographic(db):
    _seed_messages(db)
    results = db.query("/msgs").order_by_key().get()
    keys = list(results.keys())
    assert keys == sorted(keys)


def test_order_by_key_equal_to(db):
    _seed_messages(db)
    results = db.query("/msgs").order_by_key().equal_to("b").get()
    assert list(results.keys()) == ["b"]


# ---------------------------------------------------------------------------
# orderByValue (scalar children)
# ---------------------------------------------------------------------------

def test_order_by_value_on_scalar_children(db):
    db.set("/scores/p1", 30)
    db.set("/scores/p2", 10)
    db.set("/scores/p3", 20)
    results = db.query("/scores").order_by_value().get()
    vals = list(results.values())
    assert vals == sorted(vals)


def test_order_by_value_limit_to_first(db):
    db.set("/scores/p1", 30)
    db.set("/scores/p2", 10)
    db.set("/scores/p3", 20)
    results = db.query("/scores").order_by_value().limit_to_first(2).get()
    assert len(results) == 2
    assert all(v <= 20 for v in results.values())


# ---------------------------------------------------------------------------
# query on empty / non-dict path
# ---------------------------------------------------------------------------

def test_query_on_missing_path_returns_empty(db):
    results = db.query("/nonexistent").order_by_key().get()
    assert results == {}


def test_query_on_scalar_returns_empty(db):
    db.set("/flag", True)
    results = db.query("/flag").order_by_key().get()
    assert results == {}


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def test_transaction_increment(db):
    db.set("/ctr", 0)
    result = db.transaction("/ctr", lambda n: (n or 0) + 1)
    assert result == 1
    assert db.get("/ctr") == 1


def test_transaction_on_missing_path_starts_from_none(db):
    result = db.transaction("/new", lambda v: (v or 0) + 5)
    assert result == 5
    assert db.get("/new") == 5


def test_transaction_idempotent_under_lock(db):
    db.set("/x", 10)
    for _ in range(5):
        db.transaction("/x", lambda v: (v or 0) + 1)
    assert db.get("/x") == 15


def test_transaction_set_op(db):
    db.set("/flag", False)
    db.transaction("/flag", lambda _: True)
    assert db.get("/flag") is True


def test_transaction_callable_receives_current_value(db):
    db.set("/num", 42)
    received = []
    db.transaction("/num", lambda v: received.append(v) or v)
    assert received == [42]


# ---------------------------------------------------------------------------
# OnDisconnect stub
# ---------------------------------------------------------------------------

def test_on_disconnect_set(db):
    db.set("/presence/u1", "online")
    od = db.on_disconnect("/presence/u1")
    od.set("offline")
    db.simulate_disconnect("/presence/u1")
    assert db.get("/presence/u1") == "offline"


def test_on_disconnect_remove(db):
    db.set("/presence/u1", "online")
    db.on_disconnect("/presence/u1").remove()
    db.simulate_disconnect("/presence/u1")
    assert db.get("/presence/u1") is None


def test_on_disconnect_update(db):
    db.set("/presence/u1", {"status": "online", "last_seen": 0})
    db.on_disconnect("/presence/u1").update({"status": "offline"})
    db.simulate_disconnect("/presence/u1")
    doc = db.get("/presence/u1")
    assert doc["status"] == "offline"
    assert doc["last_seen"] == 0


def test_on_disconnect_cancel(db):
    db.set("/presence/u1", "online")
    od = db.on_disconnect("/presence/u1")
    od.set("offline")
    od.cancel()
    db.simulate_disconnect("/presence/u1")
    # cancel cleared ops, so value should be unchanged
    assert db.get("/presence/u1") == "online"


def test_on_disconnect_multiple_ops(db):
    db.set("/data/a", 1)
    db.set("/data/b", 2)
    od = db.on_disconnect("/data/a")
    od.set(99)
    db.on_disconnect("/data/b").remove()
    db.simulate_disconnect()  # simulate full disconnect (no path arg)
    assert db.get("/data/a") == 99
    assert db.get("/data/b") is None


def test_on_disconnect_returns_same_handler(db):
    h1 = db.on_disconnect("/x")
    h2 = db.on_disconnect("/x")
    assert h1 is h2


def test_simulate_disconnect_specific_path_only(db):
    db.set("/a", "online")
    db.set("/b", "online")
    db.on_disconnect("/a").set("offline")
    db.on_disconnect("/b").set("offline")
    db.simulate_disconnect("/a")  # only trigger /a
    assert db.get("/a") == "offline"
    assert db.get("/b") == "online"
