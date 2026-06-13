"""Unit tests for the deep Functions features added in the messaging+compute pass.

Covers:
* Callable functions (on_call / call_callable)
* FunctionError structured error
* Auth triggers (onAuthUserCreate / onAuthUserDelete)
* Storage triggers (onStorageObjectFinalize / onStorageObjectDelete)
* Pub/Sub publish + subscribe
* Scheduled function registration + run_scheduled
* Introspection endpoints for all new handler types
"""

import pytest

from openfirebase.functions import (
    FunctionRegistry, FunctionError,
    ON_CREATE, ON_WRITE, ON_UPDATE, ON_DELETE,
    ON_AUTH_USER_CREATE, ON_AUTH_USER_DELETE,
    ON_STORAGE_FINALIZE, ON_STORAGE_DELETE,
)


@pytest.fixture
def reg():
    return FunctionRegistry()


# ---- Callable functions -------------------------------------------------

def test_callable_basic(reg):
    @reg.on_call("greet")
    def greet(data, context):
        return {"hello": data.get("name")}

    result = reg.call_callable("greet", {"name": "Ada"})
    assert result == {"result": {"hello": "Ada"}}


def test_callable_context_passed(reg):
    received = {}

    @reg.on_call("ctx_check")
    def handler(data, context):
        received.update(context)
        return "ok"

    reg.call_callable("ctx_check", {}, context={"auth": {"uid": "u1"}})
    assert received.get("auth", {}).get("uid") == "u1"


def test_callable_function_error(reg):
    @reg.on_call("fail")
    def fail(data, context):
        raise FunctionError("not-allowed", code="permission-denied")

    result = reg.call_callable("fail", {})
    assert "error" in result
    assert result["error"]["code"] == "permission-denied"
    assert result["error"]["message"] == "not-allowed"


def test_callable_unhandled_exception_returns_internal_error(reg):
    @reg.on_call("crash")
    def crash(data, context):
        raise ValueError("boom")

    result = reg.call_callable("crash", {})
    assert result["error"]["code"] == "internal"


def test_callable_missing_raises_key_error(reg):
    with pytest.raises(KeyError):
        reg.call_callable("nonexistent", {})


def test_register_callable(reg):
    reg.register_callable("add", lambda data, ctx: data["a"] + data["b"])
    result = reg.call_callable("add", {"a": 3, "b": 4})
    assert result == {"result": 7}


def test_list_callable_handlers(reg):
    reg.on_call("fn1")(lambda d, c: None)
    reg.on_call("fn2")(lambda d, c: None)
    assert set(reg.list_callable_handlers()) == {"fn1", "fn2"}


# ---- Auth triggers ------------------------------------------------------

def test_auth_user_create_fires(reg):
    seen = []

    @reg.on_auth_user(ON_AUTH_USER_CREATE)
    def on_create(user):
        seen.append(user["uid"])

    reg.dispatch_auth(ON_AUTH_USER_CREATE, {"uid": "u1", "email": "a@b.com"})
    assert seen == ["u1"]


def test_auth_user_delete_fires(reg):
    seen = []

    @reg.on_auth_user(ON_AUTH_USER_DELETE)
    def on_delete(user):
        seen.append(user["uid"])

    reg.dispatch_auth(ON_AUTH_USER_DELETE, {"uid": "u2"})
    assert "u2" in seen


def test_auth_event_does_not_cross_fire(reg):
    create_calls = []
    delete_calls = []

    reg.on_auth_user(ON_AUTH_USER_CREATE)(lambda u: create_calls.append(1))
    reg.on_auth_user(ON_AUTH_USER_DELETE)(lambda u: delete_calls.append(1))

    reg.dispatch_auth(ON_AUTH_USER_CREATE, {"uid": "x"})
    assert create_calls == [1] and delete_calls == []


def test_invalid_auth_event_raises(reg):
    with pytest.raises(ValueError):
        reg.on_auth_user("onAuthWhatever")


def test_auth_handler_error_isolated(reg):
    reg.on_auth_user(ON_AUTH_USER_CREATE)(lambda u: 1 / 0)
    good = []
    reg.on_auth_user(ON_AUTH_USER_CREATE)(lambda u: good.append(1))
    reg.dispatch_auth(ON_AUTH_USER_CREATE, {"uid": "y"})
    assert good == [1]
    assert reg.errors


def test_register_auth(reg):
    calls = []
    reg.register_auth(ON_AUTH_USER_CREATE, lambda u: calls.append(u))
    reg.dispatch_auth(ON_AUTH_USER_CREATE, {"uid": "z"})
    assert calls[0]["uid"] == "z"


def test_list_auth_handlers(reg):
    @reg.on_auth_user(ON_AUTH_USER_CREATE)
    def my_handler(u):
        pass

    handlers = reg.list_auth_handlers()
    assert handlers[0]["event"] == ON_AUTH_USER_CREATE
    assert handlers[0]["name"] == "my_handler"


# ---- Storage triggers ---------------------------------------------------

def test_storage_finalize_fires(reg):
    seen = []

    @reg.on_storage(ON_STORAGE_FINALIZE)
    def on_fin(meta):
        seen.append(meta["name"])

    reg.dispatch_storage(ON_STORAGE_FINALIZE,
                         {"name": "img.png", "bucket": "mybucket", "size": 100})
    assert "img.png" in seen


def test_storage_delete_fires(reg):
    seen = []
    reg.on_storage(ON_STORAGE_DELETE)(lambda m: seen.append(m["name"]))
    reg.dispatch_storage(ON_STORAGE_DELETE, {"name": "old.txt", "bucket": "b"})
    assert "old.txt" in seen


def test_storage_bucket_prefix_filtering(reg):
    seen = []
    reg.on_storage(ON_STORAGE_FINALIZE, bucket_prefix="private-")(
        lambda m: seen.append(m["name"])
    )
    reg.dispatch_storage(ON_STORAGE_FINALIZE, {"name": "a.txt", "bucket": "public"})
    assert not seen
    reg.dispatch_storage(ON_STORAGE_FINALIZE, {"name": "b.txt", "bucket": "private-data"})
    assert "b.txt" in seen


def test_storage_event_does_not_cross_fire(reg):
    fin = []
    del_ = []
    reg.on_storage(ON_STORAGE_FINALIZE)(lambda m: fin.append(1))
    reg.on_storage(ON_STORAGE_DELETE)(lambda m: del_.append(1))
    reg.dispatch_storage(ON_STORAGE_FINALIZE, {"name": "x", "bucket": "b"})
    assert fin == [1] and del_ == []


def test_invalid_storage_event_raises(reg):
    with pytest.raises(ValueError):
        reg.on_storage("onStorageWhatever")


def test_register_storage(reg):
    calls = []
    reg.register_storage(ON_STORAGE_FINALIZE, "", lambda m: calls.append(m))
    reg.dispatch_storage(ON_STORAGE_FINALIZE, {"name": "f", "bucket": "b"})
    assert calls[0]["name"] == "f"


def test_list_storage_handlers(reg):
    @reg.on_storage(ON_STORAGE_FINALIZE, "imgs/")
    def handler(m):
        pass

    handlers = reg.list_storage_handlers()
    assert handlers[0]["event"] == ON_STORAGE_FINALIZE
    assert handlers[0]["bucket_prefix"] == "imgs/"


# ---- Pub/Sub ------------------------------------------------------------

def test_pubsub_publish_fires_subscribers(reg):
    received = []

    @reg.on_pubsub("events")
    def sub1(ctx):
        received.append(ctx["message"])

    @reg.on_pubsub("events")
    def sub2(ctx):
        received.append(ctx["message"])

    reg.publish("events", {"type": "click"})
    assert received.count({"type": "click"}) == 2


def test_pubsub_topic_isolation(reg):
    a_msgs = []
    b_msgs = []
    reg.on_pubsub("topic-a")(lambda ctx: a_msgs.append(ctx["message"]))
    reg.on_pubsub("topic-b")(lambda ctx: b_msgs.append(ctx["message"]))

    reg.publish("topic-a", "msg-a")
    assert a_msgs == ["msg-a"] and b_msgs == []


def test_pubsub_no_subscribers_returns_empty(reg):
    results = reg.publish("no-one", {"x": 1})
    assert results == []


def test_pubsub_subscriber_error_isolated(reg):
    ok = []
    reg.on_pubsub("t")(lambda ctx: 1 / 0)
    reg.on_pubsub("t")(lambda ctx: ok.append(1))
    reg.publish("t", "ping")
    assert ok == [1]
    assert reg.errors


def test_register_pubsub(reg):
    msgs = []
    reg.register_pubsub("chan", lambda ctx: msgs.append(ctx["message"]))
    reg.publish("chan", "hello")
    assert msgs == ["hello"]


def test_list_pubsub_handlers(reg):
    reg.on_pubsub("topic-x")(lambda ctx: None)
    reg.on_pubsub("topic-x")(lambda ctx: None)
    reg.on_pubsub("topic-y")(lambda ctx: None)
    info = reg.list_pubsub_handlers()
    assert len(info["topic-x"]) == 2
    assert len(info["topic-y"]) == 1


def test_publish_includes_topic_and_time(reg):
    ctx_received = {}
    reg.on_pubsub("t")(lambda ctx: ctx_received.update(ctx))
    reg.publish("t", "data")
    assert ctx_received["topic"] == "t"
    assert ctx_received["message"] == "data"
    assert "published_at" in ctx_received


# ---- Scheduled functions ------------------------------------------------

def test_schedule_and_run(reg):
    results = []

    @reg.schedule("nightly", cron="0 0 * * *")
    def nightly(ctx):
        results.append("ran")
        return "done"

    val = reg.run_scheduled("nightly")
    assert val == "done"
    assert results == ["ran"]


def test_scheduled_receives_context(reg):
    ctx_received = {}

    @reg.schedule("check")
    def check(ctx):
        ctx_received.update(ctx)

    reg.run_scheduled("check")
    assert "scheduled_time" in ctx_received


def test_run_scheduled_missing_raises(reg):
    with pytest.raises(KeyError):
        reg.run_scheduled("does-not-exist")


def test_register_schedule(reg):
    calls = []
    reg.register_schedule("job1", lambda ctx: calls.append(ctx), cron="* * * * *")
    reg.run_scheduled("job1")
    assert calls


def test_list_scheduled(reg):
    reg.schedule("job-a", "0 8 * * 1")(lambda ctx: None)
    reg.schedule("job-b")(lambda ctx: None)
    info = reg.list_scheduled()
    names = {e["name"] for e in info}
    assert {"job-a", "job-b"} <= names
    # last_run is None before first execution
    a = next(e for e in info if e["name"] == "job-a")
    assert a["last_run"] is None
    reg.run_scheduled("job-a")
    info2 = reg.list_scheduled()
    a2 = next(e for e in info2 if e["name"] == "job-a")
    assert a2["last_run"] is not None


def test_schedule_error_propagates_and_logged(reg):
    @reg.schedule("crash")
    def crash(ctx):
        raise RuntimeError("scheduled boom")

    with pytest.raises(RuntimeError):
        reg.run_scheduled("crash")
    assert reg.errors


# ---- Introspection ------------------------------------------------------

def test_full_introspection(reg):
    reg.on_db(ON_CREATE)(lambda ctx: None)
    reg.on_request("api")(lambda req: None)
    reg.on_call("fn")(lambda d, c: None)
    reg.on_auth_user(ON_AUTH_USER_CREATE)(lambda u: None)
    reg.on_storage(ON_STORAGE_FINALIZE)(lambda m: None)
    reg.on_pubsub("events")(lambda ctx: None)
    reg.schedule("daily")(lambda ctx: None)

    assert reg.list_http_handlers() == ["api"]
    assert reg.list_callable_handlers() == ["fn"]
    assert len(reg.list_db_handlers()) == 1
    assert len(reg.list_auth_handlers()) == 1
    assert len(reg.list_storage_handlers()) == 1
    assert "events" in reg.list_pubsub_handlers()
    assert any(e["name"] == "daily" for e in reg.list_scheduled())
