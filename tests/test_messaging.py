"""Unit tests for Cloud Messaging (openfirebase.messaging)."""

import pytest

from openfirebase.messaging import CloudMessaging, MessagingError
from openfirebase.storage import MemoryStore


class TestCloudMessaging:
    def setup_method(self):
        self.msg = CloudMessaging(MemoryStore())

    # ---- token management ---------------------------------------------------

    def test_register_token(self):
        rec = self.msg.register_token("tok1", metadata={"platform": "android"})
        assert rec["token"] == "tok1"
        assert rec["metadata"]["platform"] == "android"

    def test_get_token(self):
        self.msg.register_token("tok2")
        rec = self.msg.get_token("tok2")
        assert rec is not None
        assert rec["token"] == "tok2"

    def test_get_unknown_token(self):
        assert self.msg.get_token("nope") is None

    def test_list_tokens(self):
        self.msg.register_token("t1")
        self.msg.register_token("t2")
        tokens = self.msg.list_tokens()
        token_vals = [t["token"] for t in tokens]
        assert "t1" in token_vals
        assert "t2" in token_vals

    def test_unregister_token(self):
        self.msg.register_token("tok3")
        ok = self.msg.unregister_token("tok3")
        assert ok is True
        assert self.msg.get_token("tok3") is None

    def test_unregister_nonexistent(self):
        assert self.msg.unregister_token("ghost") is False

    def test_register_empty_token_raises(self):
        with pytest.raises(MessagingError):
            self.msg.register_token("")

    def test_unregister_removes_from_topics(self):
        self.msg.register_token("t_sub")
        self.msg.subscribe("t_sub", "news")
        assert "t_sub" in self.msg.topic_subscribers("news")
        self.msg.unregister_token("t_sub")
        assert "t_sub" not in self.msg.topic_subscribers("news")

    # ---- topic management ---------------------------------------------------

    def test_subscribe(self):
        self.msg.subscribe("tok_a", "sports")
        subs = self.msg.topic_subscribers("sports")
        assert "tok_a" in subs

    def test_subscribe_idempotent(self):
        self.msg.subscribe("tok_b", "news")
        self.msg.subscribe("tok_b", "news")
        subs = self.msg.topic_subscribers("news")
        assert subs.count("tok_b") == 1

    def test_unsubscribe(self):
        self.msg.subscribe("tok_c", "weather")
        ok = self.msg.unsubscribe("tok_c", "weather")
        assert ok is True
        assert "tok_c" not in self.msg.topic_subscribers("weather")

    def test_unsubscribe_not_subscribed(self):
        ok = self.msg.unsubscribe("phantom", "topic_x")
        assert ok is False

    def test_list_topics(self):
        self.msg.subscribe("t1", "alpha")
        self.msg.subscribe("t2", "beta")
        topics = self.msg.list_topics()
        names = {t["topic"] for t in topics}
        assert "alpha" in names
        assert "beta" in names

    def test_get_topic(self):
        self.msg.subscribe("t1", "music")
        topic = self.msg.get_topic("music")
        assert topic is not None
        assert "t1" in topic["tokens"]

    def test_get_unknown_topic(self):
        assert self.msg.get_topic("nonexistent") is None

    def test_subscribe_empty_raises(self):
        with pytest.raises(MessagingError):
            self.msg.subscribe("", "topic")
        with pytest.raises(MessagingError):
            self.msg.subscribe("tok", "")

    # ---- send to token ------------------------------------------------------

    def test_send_to_token(self):
        rec = self.msg.send_to_token(
            "device_tok",
            notification={"title": "Hi", "body": "Hello"},
            data={"key": "val"},
        )
        assert rec["target_type"] == "token"
        assert rec["target"] == "device_tok"
        assert rec["notification"]["title"] == "Hi"
        assert rec["data"]["key"] == "val"
        assert "message_id" in rec

    def test_send_to_empty_token_raises(self):
        with pytest.raises(MessagingError):
            self.msg.send_to_token("")

    def test_send_stored_in_inbox(self):
        rec = self.msg.send_to_token("mytok")
        fetched = self.msg.get_message(rec["message_id"])
        assert fetched is not None
        assert fetched["message_id"] == rec["message_id"]

    # ---- send to topic ------------------------------------------------------

    def test_send_to_topic(self):
        self.msg.subscribe("subscriber1", "breaking")
        self.msg.subscribe("subscriber2", "breaking")
        rec = self.msg.send_to_topic("breaking",
                                     notification={"title": "News"})
        assert rec["target_type"] == "topic"
        assert rec["target"] == "breaking"
        assert "subscriber1" in rec["recipients"]
        assert "subscriber2" in rec["recipients"]

    def test_send_to_topic_no_subscribers(self):
        rec = self.msg.send_to_topic("empty_topic")
        assert rec["recipients"] == []

    def test_send_to_empty_topic_raises(self):
        with pytest.raises(MessagingError):
            self.msg.send_to_topic("")

    # ---- multicast ----------------------------------------------------------

    def test_send_multicast(self):
        rec = self.msg.send_multicast(["tok1", "tok2", "tok3"],
                                      notification={"title": "Broadcast"})
        assert rec["target_type"] == "multicast"
        assert set(rec["target"]) == {"tok1", "tok2", "tok3"}

    def test_send_multicast_empty_raises(self):
        with pytest.raises(MessagingError):
            self.msg.send_multicast([])

    # ---- inbox / listing ----------------------------------------------------

    def test_list_messages(self):
        self.msg.send_to_token("t1")
        self.msg.send_to_token("t2")
        msgs = self.msg.list_messages()
        assert len(msgs) >= 2

    def test_list_messages_filter_by_target(self):
        self.msg.send_to_token("only_this")
        self.msg.send_to_token("not_this")
        msgs = self.msg.list_messages(target="only_this")
        assert all(m["target"] == "only_this" for m in msgs)

    def test_list_messages_filter_by_type(self):
        self.msg.send_to_token("tok")
        self.msg.send_to_topic("tp")
        token_msgs = self.msg.list_messages(target_type="token")
        assert all(m["target_type"] == "token" for m in token_msgs)

    def test_list_messages_limit(self):
        for i in range(5):
            self.msg.send_to_token(f"t{i}")
        msgs = self.msg.list_messages(limit=3)
        assert len(msgs) <= 3

    def test_clear_inbox(self):
        self.msg.send_to_token("tok")
        self.msg.send_to_topic("tp")
        count = self.msg.clear_inbox()
        assert count >= 2
        assert self.msg.list_messages() == []

    def test_data_values_are_strings(self):
        rec = self.msg.send_to_token("t",
                                     data={"count": 5, "flag": True})  # type: ignore[arg-type]
        assert rec["data"]["count"] == "5"
        assert rec["data"]["flag"] == "True"
