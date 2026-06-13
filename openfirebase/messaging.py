"""Cloud Messaging (FCM) local emulator.

Provides:
* **Token registry** — register device tokens (mapping token → metadata).
* **Topic subscriptions** — subscribe/unsubscribe tokens to named topics.
* **Send to token** — record a message targeting a specific device token.
* **Send to topic** — broadcast to all tokens subscribed to a topic.
* **Message inbox** — retrieve captured messages (for testing/assertions).

Messages are NOT delivered to real devices.  The inbox lets tests verify that
the right messages were sent.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from .storage import BaseStore, MemoryStore

_NS_TOKENS    = "messaging::tokens"
_NS_TOPICS    = "messaging::topics"    # topic → list of token strings
_NS_INBOX     = "messaging::inbox"     # message_id → message record
_NS_SENT      = "messaging::sent_idx"  # index key → message_id list per token


class MessagingError(Exception):
    """Raised for invalid messaging operations."""


class CloudMessaging:
    """Local FCM emulator.

    Parameters
    ----------
    store:
        Shared BaseStore.  Defaults to MemoryStore.
    """

    def __init__(self, store: Optional[BaseStore] = None) -> None:
        self._store = store if store is not None else MemoryStore()

    # ---- token management ---------------------------------------------------

    def register_token(self, token: str,
                       metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Register a device token.

        Parameters
        ----------
        token:
            Opaque device token string.
        metadata:
            Optional dict (e.g. ``{"platform": "android", "app_id": "com.example"}``).
        """
        if not token:
            raise MessagingError("token must not be empty")
        record = {
            "token": token,
            "registered_at": time.time(),
            "metadata": metadata or {},
        }
        self._store.set(_NS_TOKENS, token, record)
        return record

    def unregister_token(self, token: str) -> bool:
        """Remove a device token and its topic subscriptions."""
        ok = self._store.delete(_NS_TOKENS, token)
        if ok:
            # remove from all topics
            for topic_name, topic_data in list(self._store.items(_NS_TOPICS)):
                tokens = topic_data.get("tokens", [])
                if token in tokens:
                    tokens = [t for t in tokens if t != token]
                    self._store.set(_NS_TOPICS, topic_name,
                                    dict(topic_data, tokens=tokens))
        return ok

    def get_token(self, token: str) -> Optional[Dict[str, Any]]:
        return self._store.get(_NS_TOKENS, token)

    def list_tokens(self) -> List[Dict[str, Any]]:
        return [v for _, v in self._store.items(_NS_TOKENS)]

    # ---- topic management ---------------------------------------------------

    def subscribe(self, token: str, topic: str) -> None:
        """Subscribe *token* to *topic*.

        The token does not need to be registered first.
        """
        if not token or not topic:
            raise MessagingError("token and topic must not be empty")
        existing = self._store.get(_NS_TOPICS, topic) or {"topic": topic, "tokens": []}
        tokens = existing.get("tokens", [])
        if token not in tokens:
            tokens = tokens + [token]
        self._store.set(_NS_TOPICS, topic, dict(existing, tokens=tokens))

    def unsubscribe(self, token: str, topic: str) -> bool:
        """Unsubscribe *token* from *topic*. Returns True if it was subscribed."""
        existing = self._store.get(_NS_TOPICS, topic)
        if not existing:
            return False
        tokens = existing.get("tokens", [])
        if token not in tokens:
            return False
        tokens = [t for t in tokens if t != token]
        self._store.set(_NS_TOPICS, topic, dict(existing, tokens=tokens))
        return True

    def get_topic(self, topic: str) -> Optional[Dict[str, Any]]:
        return self._store.get(_NS_TOPICS, topic)

    def list_topics(self) -> List[Dict[str, Any]]:
        return [v for _, v in self._store.items(_NS_TOPICS)]

    def topic_subscribers(self, topic: str) -> List[str]:
        t = self._store.get(_NS_TOPICS, topic)
        return t.get("tokens", []) if t else []

    # ---- send ---------------------------------------------------------------

    def send_to_token(self, token: str,
                      notification: Optional[Dict[str, Any]] = None,
                      data: Optional[Dict[str, str]] = None,
                      **kwargs) -> Dict[str, Any]:
        """Record a message sent to a specific device token.

        Parameters
        ----------
        token:
            Target device token.
        notification:
            Optional ``{"title": str, "body": str}`` dict.
        data:
            Optional ``{key: str}`` data payload.
        **kwargs:
            Extra fields stored verbatim (e.g. ``android``, ``apns``).

        Returns the stored message record including its ``message_id``.
        """
        if not token:
            raise MessagingError("token must not be empty")
        msg_id = uuid.uuid4().hex
        record = {
            "message_id": msg_id,
            "target_type": "token",
            "target": token,
            "notification": notification or {},
            "data": {str(k): str(v) for k, v in (data or {}).items()},
            "sent_at": time.time(),
            **kwargs,
        }
        self._store.set(_NS_INBOX, msg_id, record)
        return record

    def send_to_topic(self, topic: str,
                      notification: Optional[Dict[str, Any]] = None,
                      data: Optional[Dict[str, str]] = None,
                      **kwargs) -> Dict[str, Any]:
        """Record a message broadcast to a topic.

        All tokens currently subscribed to the topic are recorded in the
        message.  Returns the stored message record.
        """
        if not topic:
            raise MessagingError("topic must not be empty")
        msg_id = uuid.uuid4().hex
        subscribers = self.topic_subscribers(topic)
        record = {
            "message_id": msg_id,
            "target_type": "topic",
            "target": topic,
            "recipients": subscribers,
            "notification": notification or {},
            "data": {str(k): str(v) for k, v in (data or {}).items()},
            "sent_at": time.time(),
            **kwargs,
        }
        self._store.set(_NS_INBOX, msg_id, record)
        return record

    def send_multicast(self, tokens: List[str],
                       notification: Optional[Dict[str, Any]] = None,
                       data: Optional[Dict[str, str]] = None,
                       **kwargs) -> Dict[str, Any]:
        """Record a message sent to multiple tokens."""
        if not tokens:
            raise MessagingError("tokens list must not be empty")
        msg_id = uuid.uuid4().hex
        record = {
            "message_id": msg_id,
            "target_type": "multicast",
            "target": tokens,
            "notification": notification or {},
            "data": {str(k): str(v) for k, v in (data or {}).items()},
            "sent_at": time.time(),
            **kwargs,
        }
        self._store.set(_NS_INBOX, msg_id, record)
        return record

    # ---- inbox / retrieval --------------------------------------------------

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get(_NS_INBOX, message_id)

    def list_messages(self,
                      target: Optional[str] = None,
                      target_type: Optional[str] = None,
                      limit: int = 100) -> List[Dict[str, Any]]:
        """Return captured messages, newest first.

        Parameters
        ----------
        target:
            Filter by token or topic.
        target_type:
            Filter by ``"token"``, ``"topic"``, or ``"multicast"``.
        limit:
            Maximum number of messages to return.
        """
        msgs = [v for _, v in self._store.items(_NS_INBOX)]
        msgs.sort(key=lambda m: m.get("sent_at", 0), reverse=True)
        if target is not None:
            msgs = [m for m in msgs
                    if m.get("target") == target
                    or (isinstance(m.get("target"), list) and target in m["target"])
                    or m.get("recipients") and target in m["recipients"]]
        if target_type is not None:
            msgs = [m for m in msgs if m.get("target_type") == target_type]
        return msgs[:limit]

    def clear_inbox(self) -> int:
        """Delete all stored messages.  Returns the count deleted."""
        msgs = list(self._store.items(_NS_INBOX))
        for k, _ in msgs:
            self._store.delete(_NS_INBOX, k)
        return len(msgs)
