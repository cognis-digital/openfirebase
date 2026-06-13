"""Tests for the deepened Firestore features:
- composite (AND) where filters
- multi-key order_by
- cursor pagination (start_after / start_at / end_before / end_at)
- FieldValue sentinels (increment / arrayUnion / arrayRemove / serverTimestamp / delete)
- subcollections
- batched writes (WriteBatch)
- transactions (run_transaction / TransactionError)
"""

import time

import pytest

from openfirebase.firestore import (
    Firestore,
    FieldValue,
    WriteBatch,
    TransactionError,
)


@pytest.fixture
def fs():
    return Firestore()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed(fs):
    fs.set("p", "1", {"name": "apple",  "price": 3, "tags": ["fruit", "red"],   "cat": "A"})
    fs.set("p", "2", {"name": "banana", "price": 1, "tags": ["fruit", "yellow"],"cat": "B"})
    fs.set("p", "3", {"name": "carrot", "price": 2, "tags": ["veg"],             "cat": "A"})
    fs.set("p", "4", {"name": "date",   "price": 5, "tags": ["fruit"],           "cat": "B"})


# ---------------------------------------------------------------------------
# Composite AND filters
# ---------------------------------------------------------------------------

def test_composite_and_filter(fs):
    _seed(fs)
    rows = (fs.collection("p")
            .where("cat", "==", "B")
            .where("price", ">", 1)
            .stream())
    assert {r["name"] for r in rows} == {"date"}


def test_array_contains_any(fs):
    _seed(fs)
    rows = fs.collection("p").where("tags", "array-contains-any", ["red", "veg"]).stream()
    assert {r["name"] for r in rows} == {"apple", "carrot"}


def test_not_in_operator(fs):
    _seed(fs)
    rows = fs.collection("p").where("cat", "not-in", ["B"]).stream()
    assert all(r["cat"] == "A" for r in rows)


def test_neq_operator(fs):
    _seed(fs)
    rows = fs.collection("p").where("name", "!=", "apple").stream()
    names = {r["name"] for r in rows}
    assert "apple" not in names
    assert len(names) == 3


# ---------------------------------------------------------------------------
# Multi-key order_by + limit_to_last
# ---------------------------------------------------------------------------

def test_order_by_single_asc(fs):
    _seed(fs)
    rows = fs.collection("p").order_by("price", "asc").stream()
    prices = [r["price"] for r in rows]
    assert prices == sorted(prices)


def test_order_by_desc(fs):
    _seed(fs)
    rows = fs.collection("p").order_by("price", "desc").stream()
    prices = [r["price"] for r in rows]
    assert prices == sorted(prices, reverse=True)


def test_limit_to_last(fs):
    _seed(fs)
    rows = fs.collection("p").order_by("price", "asc").limit_to_last(2).stream()
    assert len(rows) == 2
    prices = [r["price"] for r in rows]
    assert prices == [3, 5]


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------

def test_start_after_doc_snapshot(fs):
    _seed(fs)
    # get first page (limit 2, ordered by price asc)
    page1 = fs.collection("p").order_by("price", "asc").limit(2).stream()
    assert len(page1) == 2
    last = page1[-1]
    # second page starts AFTER the last doc of page1
    page2 = fs.collection("p").order_by("price", "asc").start_after(last).stream()
    p1_prices = {r["price"] for r in page1}
    for r in page2:
        assert r["price"] not in p1_prices


def test_start_at_value(fs):
    _seed(fs)
    rows = fs.collection("p").order_by("price", "asc").start_at(2).stream()
    assert all(r["price"] >= 2 for r in rows)


def test_end_before_value(fs):
    _seed(fs)
    rows = fs.collection("p").order_by("price", "asc").end_before(3).stream()
    assert all(r["price"] < 3 for r in rows)


def test_end_at_value(fs):
    _seed(fs)
    rows = fs.collection("p").order_by("price", "asc").end_at(3).stream()
    assert all(r["price"] <= 3 for r in rows)


def test_cursor_range(fs):
    _seed(fs)
    rows = (fs.collection("p")
            .order_by("price", "asc")
            .start_at(2).end_at(3)
            .stream())
    assert {r["price"] for r in rows} == {2, 3}


# ---------------------------------------------------------------------------
# FieldValue: increment
# ---------------------------------------------------------------------------

def test_field_value_increment_new_field(fs):
    fs.set("c", "d", {"x": 10})
    fs.update("c", "d", {"x": FieldValue.increment(5)})
    assert fs.get("c", "d")["x"] == 15


def test_field_value_increment_from_zero(fs):
    fs.set("c", "d", {})
    fs.update("c", "d", {"count": FieldValue.increment(1)})
    assert fs.get("c", "d")["count"] == 1


def test_field_value_increment_negative(fs):
    fs.set("c", "d", {"score": 100})
    fs.update("c", "d", {"score": FieldValue.increment(-20)})
    assert fs.get("c", "d")["score"] == 80


# ---------------------------------------------------------------------------
# FieldValue: arrayUnion / arrayRemove
# ---------------------------------------------------------------------------

def test_field_value_array_union(fs):
    fs.set("c", "d", {"tags": ["a", "b"]})
    fs.update("c", "d", {"tags": FieldValue.array_union(["b", "c"])})
    assert fs.get("c", "d")["tags"] == ["a", "b", "c"]


def test_field_value_array_union_on_missing_field(fs):
    fs.set("c", "d", {})
    fs.update("c", "d", {"tags": FieldValue.array_union(["x"])})
    assert fs.get("c", "d")["tags"] == ["x"]


def test_field_value_array_remove(fs):
    fs.set("c", "d", {"tags": ["a", "b", "c"]})
    fs.update("c", "d", {"tags": FieldValue.array_remove(["b"])})
    assert fs.get("c", "d")["tags"] == ["a", "c"]


def test_field_value_array_remove_nonexistent(fs):
    fs.set("c", "d", {"tags": ["a"]})
    fs.update("c", "d", {"tags": FieldValue.array_remove(["nope"])})
    assert fs.get("c", "d")["tags"] == ["a"]


# ---------------------------------------------------------------------------
# FieldValue: serverTimestamp
# ---------------------------------------------------------------------------

def test_field_value_server_timestamp(fs):
    before = time.time()
    fs.set("c", "d", {"ts": FieldValue.server_timestamp()})
    doc = fs.get("c", "d")
    assert before <= doc["ts"] <= time.time()


# ---------------------------------------------------------------------------
# FieldValue: delete
# ---------------------------------------------------------------------------

def test_field_value_delete_removes_field(fs):
    fs.set("c", "d", {"keep": 1, "gone": 2})
    fs.update("c", "d", {"gone": FieldValue.delete()})
    doc = fs.get("c", "d")
    assert "gone" not in doc
    assert doc["keep"] == 1


# ---------------------------------------------------------------------------
# FieldValue in set() with merge
# ---------------------------------------------------------------------------

def test_field_value_in_set_merge(fs):
    fs.set("c", "d", {"score": 10, "tags": ["x"]})
    fs.set("c", "d", {
        "score": FieldValue.increment(5),
        "tags":  FieldValue.array_union(["y"]),
    }, merge=True)
    doc = fs.get("c", "d")
    assert doc["score"] == 15
    assert set(doc["tags"]) == {"x", "y"}


# ---------------------------------------------------------------------------
# Subcollections
# ---------------------------------------------------------------------------

def test_subcollection_isolated_from_parent(fs):
    fs.set("users", "u1", {"name": "Ada"})
    fs.set("users/u1/posts", "p1", {"title": "Hello"})
    # subcollection doc should not appear in parent
    parent_docs = fs.list("users")
    assert all(d.get("title") is None for d in parent_docs)
    # subcollection accessible via full path
    sub_doc = fs.get("users/u1/posts", "p1")
    assert sub_doc["title"] == "Hello"


def test_subcollection_query(fs):
    fs.set("users/u1/orders", "o1", {"amount": 100})
    fs.set("users/u1/orders", "o2", {"amount": 50})
    rows = fs.subcollection("users/u1/orders").where("amount", ">", 60).stream()
    assert len(rows) == 1 and rows[0]["amount"] == 100


def test_subcollection_add_and_list(fs):
    oid = fs.add("groups/g1/members", {"email": "a@b.com"})
    docs = fs.list("groups/g1/members")
    assert any(d["email"] == "a@b.com" for d in docs)


# ---------------------------------------------------------------------------
# WriteBatch
# ---------------------------------------------------------------------------

def test_batch_set_update_delete(fs):
    fs.set("c", "d1", {"v": 1})
    fs.set("c", "d2", {"v": 2})
    batch = fs.batch()
    batch.set("c", "d3", {"v": 3})
    batch.update("c", "d1", {"v": 10})
    batch.delete("c", "d2")
    batch.commit()
    assert fs.get("c", "d3")["v"] == 3
    assert fs.get("c", "d1")["v"] == 10
    assert fs.get("c", "d2") is None


def test_batch_all_or_nothing(fs):
    """After commit all ops are applied; the batch is cleared."""
    batch = fs.batch()
    batch.set("c", "a", {"x": 1})
    batch.set("c", "b", {"x": 2})
    batch.commit()
    assert fs.get("c", "a") is not None
    assert fs.get("c", "b") is not None
    # second commit of empty batch is a no-op
    batch.commit()


def test_batch_chaining(fs):
    result = fs.batch().set("c", "x", {"v": 1}).update("c", "x", {"v": 2})
    assert isinstance(result, WriteBatch)
    result.commit()
    assert fs.get("c", "x")["v"] == 2


def test_batch_with_field_values(fs):
    fs.set("c", "counter", {"n": 0})
    batch = fs.batch()
    batch.update("c", "counter", {"n": FieldValue.increment(10)})
    batch.commit()
    assert fs.get("c", "counter")["n"] == 10


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def test_transaction_basic_transfer(fs):
    fs.set("accounts", "alice", {"balance": 100})
    fs.set("accounts", "bob",   {"balance": 50})

    def transfer(txn):
        alice = txn.get("accounts", "alice")
        bob   = txn.get("accounts", "bob")
        txn.update("accounts", "alice", {"balance": alice["balance"] - 30})
        txn.update("accounts", "bob",   {"balance": bob["balance"] + 30})

    fs.run_transaction(transfer)
    assert fs.get("accounts", "alice")["balance"] == 70
    assert fs.get("accounts", "bob")["balance"]   == 80


def test_transaction_set_and_delete(fs):
    fs.set("c", "existing", {"v": 1})

    def txn_fn(txn):
        txn.set("c", "new_doc", {"v": 99})
        txn.delete("c", "existing")

    fs.run_transaction(txn_fn)
    assert fs.get("c", "new_doc")["v"] == 99
    assert fs.get("c", "existing") is None


def test_transaction_read_returns_doc(fs):
    fs.set("c", "d", {"val": 42})

    read_val = {}

    def txn_fn(txn):
        doc = txn.get("c", "d")
        read_val["val"] = doc["val"]
        txn.update("c", "d", {"val": doc["val"] + 1})

    fs.run_transaction(txn_fn)
    assert read_val["val"] == 42
    assert fs.get("c", "d")["val"] == 43


def test_transaction_retries_on_conflict(fs):
    """A second writer sneaking in between read and commit forces a retry."""
    fs.set("c", "ctr", {"n": 0})
    call_count = [0]

    def txn_fn(txn):
        call_count[0] += 1
        doc = txn.get("c", "ctr")
        if call_count[0] == 1:
            # simulate external write between read and commit
            fs.set("c", "ctr", {"n": 99})
        txn.update("c", "ctr", {"n": doc["n"] + 1})

    fs.run_transaction(txn_fn, max_attempts=3)
    # On the retry, read_val was 99, so final value is 100
    assert fs.get("c", "ctr")["n"] == 100
    assert call_count[0] == 2


def test_transaction_aborts_after_max_attempts(fs):
    fs.set("c", "x", {"n": 0})

    def txn_fn(txn):
        txn.get("c", "x")
        # always corrupt the doc so conflict never resolves
        fs.set("c", "x", {"n": 999})
        txn.update("c", "x", {"n": 1})

    with pytest.raises(TransactionError):
        fs.run_transaction(txn_fn, max_attempts=2)
