import pytest

from openfirebase.functions import (FunctionRegistry, ON_CREATE, ON_WRITE,
                                     ON_DELETE, ON_UPDATE)


@pytest.fixture
def reg():
    return FunctionRegistry()


def test_oncreate_fires(reg):
    seen = []

    @reg.on_db(ON_CREATE)
    def handler(ctx):
        seen.append(ctx)
        return ctx["after"]

    out = reg.dispatch_db(ON_CREATE, "users/u1", None, {"name": "Ada"})
    assert seen and seen[0]["path"] == "users/u1"
    assert out == [{"name": "Ada"}]


def test_onwrite_fires_for_create_update_delete(reg):
    fired = []

    @reg.on_db(ON_WRITE)
    def handler(ctx):
        fired.append(ctx["event"])

    reg.dispatch_db(ON_CREATE, "x/1", None, {"a": 1})
    reg.dispatch_db(ON_UPDATE, "x/1", {"a": 1}, {"a": 2})
    reg.dispatch_db(ON_DELETE, "x/1", {"a": 2}, None)
    assert fired == [ON_CREATE, ON_UPDATE, ON_DELETE]


def test_path_prefix_filtering(reg):
    fired = []

    @reg.on_db(ON_CREATE, path_prefix="orders/")
    def handler(ctx):
        fired.append(ctx["path"])

    reg.dispatch_db(ON_CREATE, "users/1", None, {})
    reg.dispatch_db(ON_CREATE, "orders/99", None, {})
    assert fired == ["orders/99"]


def test_handler_error_isolated(reg):
    @reg.on_db(ON_CREATE)
    def bad(ctx):
        raise RuntimeError("boom")

    good_calls = []

    @reg.on_db(ON_CREATE)
    def good(ctx):
        good_calls.append(1)
        return "ok"

    out = reg.dispatch_db(ON_CREATE, "x/1", None, {})
    assert out == ["ok"]
    assert good_calls == [1]
    assert reg.errors and reg.errors[0]["handler"] == "bad"


def test_on_request(reg):
    @reg.on_request("hello")
    def hello(req):
        return {"echo": req["body"].get("name")}

    res = reg.call_request("hello", {"method": "POST", "body": {"name": "Bo"},
                                     "query": {}})
    assert res == {"echo": "Bo"}


def test_call_unknown_request_raises(reg):
    with pytest.raises(KeyError):
        reg.call_request("missing", {})


def test_invalid_event_raises(reg):
    with pytest.raises(ValueError):
        reg.on_db("onWhatever")


def test_introspection(reg):
    reg.on_db(ON_CREATE, "a/")(lambda c: None)
    reg.on_request("fn")(lambda r: None)
    assert reg.list_http_handlers() == ["fn"]
    db = reg.list_db_handlers()
    assert db[0]["event"] == ON_CREATE and db[0]["path_prefix"] == "a/"
