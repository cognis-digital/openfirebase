import pytest

from openfirebase.firestore import Firestore


@pytest.fixture
def fs():
    return Firestore()


def test_add_and_get(fs):
    doc_id = fs.add("users", {"name": "Ada", "age": 36})
    doc = fs.get("users", doc_id)
    assert doc["name"] == "Ada"
    assert doc["age"] == 36
    assert doc["id"] == doc_id
    assert "_created_at" in doc and "_updated_at" in doc


def test_set_explicit_id(fs):
    fs.set("cities", "LA", {"name": "Los Angeles", "pop": 4_000_000})
    assert fs.get("cities", "LA")["pop"] == 4_000_000


def test_get_missing(fs):
    assert fs.get("users", "nope") is None


def test_update_patches_fields(fs):
    fs.set("users", "u1", {"name": "Bo", "age": 20})
    assert fs.update("users", "u1", {"age": 21}) is True
    doc = fs.get("users", "u1")
    assert doc["age"] == 21
    assert doc["name"] == "Bo"


def test_update_missing_returns_false(fs):
    assert fs.update("users", "ghost", {"x": 1}) is False


def test_set_merge(fs):
    fs.set("users", "u1", {"name": "Bo", "age": 20})
    fs.set("users", "u1", {"city": "NYC"}, merge=True)
    doc = fs.get("users", "u1")
    assert doc["name"] == "Bo"
    assert doc["city"] == "NYC"


def test_set_overwrite_without_merge(fs):
    fs.set("users", "u1", {"name": "Bo", "age": 20})
    fs.set("users", "u1", {"name": "Cy"})
    doc = fs.get("users", "u1")
    assert doc["name"] == "Cy"
    assert "age" not in doc


def test_delete(fs):
    fs.set("users", "u1", {"name": "Bo"})
    assert fs.delete("users", "u1") is True
    assert fs.get("users", "u1") is None
    assert fs.delete("users", "u1") is False


def test_exists(fs):
    assert fs.exists("c", "x") is False
    fs.set("c", "x", {"v": 1})
    assert fs.exists("c", "x") is True


def _seed(fs):
    fs.set("p", "1", {"name": "apple", "price": 3, "tags": ["fruit", "red"]})
    fs.set("p", "2", {"name": "banana", "price": 1, "tags": ["fruit", "yellow"]})
    fs.set("p", "3", {"name": "carrot", "price": 2, "tags": ["veg"]})
    fs.set("p", "4", {"name": "date", "price": 5, "tags": ["fruit"]})


def test_where_eq(fs):
    _seed(fs)
    rows = fs.where("p", "name", "==", "banana").stream()
    assert len(rows) == 1 and rows[0]["price"] == 1


def test_where_gt(fs):
    _seed(fs)
    rows = fs.collection("p").where("price", ">", 2).stream()
    assert {r["name"] for r in rows} == {"apple", "date"}


def test_where_in(fs):
    _seed(fs)
    rows = fs.collection("p").where("name", "in", ["apple", "carrot"]).stream()
    assert {r["name"] for r in rows} == {"apple", "carrot"}


def test_where_array_contains(fs):
    _seed(fs)
    rows = fs.collection("p").where("tags", "array-contains", "fruit").stream()
    assert {r["name"] for r in rows} == {"apple", "banana", "date"}


def test_chained_where(fs):
    _seed(fs)
    rows = (fs.collection("p")
            .where("tags", "array-contains", "fruit")
            .where("price", ">=", 3).stream())
    assert {r["name"] for r in rows} == {"apple", "date"}


def test_order_by_and_limit(fs):
    _seed(fs)
    rows = fs.collection("p").order_by("price", "desc").limit(2).stream()
    assert [r["name"] for r in rows] == ["date", "apple"]


def test_bad_operator_raises(fs):
    with pytest.raises(ValueError):
        fs.collection("p").where("x", "~=", 1)


def test_set_rejects_non_dict(fs):
    with pytest.raises(TypeError):
        fs.set("c", "x", [1, 2, 3])
