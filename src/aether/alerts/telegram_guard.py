"""S7 inbound guard. `is_owner` runs before ANY handler; everything else is dropped silently.

An update is the owner's only if `from.id == TELEGRAM_ALLOWED_USER_ID` **and** `chat.type ==
"private"` **and** `chat.id == TELEGRAM_ALLOWED_USER_ID`. Only plain messages qualify: callback
queries, inline queries, channel posts and membership updates are never owner commands.

Drops are logged with the sender ID and chat type only (never the text), rate-limited.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

# Update fields that carry a Message (or ChatMemberUpdated) with a `chat`.
_CHAT_FIELDS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
)
_OWNER_FIELDS = ("message", "edited_message")
_SHARED_CHAT_TYPES = frozenset({"group", "supergroup", "channel"})


def _int(v: object) -> int | None:
    # bool is an int subclass; a JSON `true` is never an ID.
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _mapping(v: object) -> Mapping[str, Any] | None:
    return v if isinstance(v, Mapping) else None


def is_owner(update: Mapping[str, Any], owner_id: int) -> bool:
    """True only for a private message from the owner in the owner's own chat."""
    present = [k for k in update if k != "update_id"]
    if len(present) != 1 or present[0] not in _OWNER_FIELDS:
        return False
    msg = _mapping(update[present[0]])
    if msg is None:
        return False
    sender = _mapping(msg.get("from"))
    chat = _mapping(msg.get("chat"))
    if sender is None or chat is None:
        return False
    return (
        _int(sender.get("id")) == owner_id
        and chat.get("type") == "private"
        and _int(chat.get("id")) == owner_id
    )


def chats_to_leave(update: Mapping[str, Any]) -> set[int]:
    """Group, supergroup or channel chats this update shows the bot in. The bot leaves them all."""
    out: set[int] = set()
    for field in _CHAT_FIELDS:
        obj = _mapping(update.get(field))
        chat = _mapping(obj.get("chat")) if obj else None
        if chat is None or chat.get("type") not in _SHARED_CHAT_TYPES:
            continue
        if field == "my_chat_member":
            new = _mapping(obj.get("new_chat_member")) if obj else None
            if new is not None and new.get("status") in ("left", "kicked"):
                continue  # already out
        chat_id = _int(chat.get("id"))
        if chat_id is not None:
            out.add(chat_id)
    return out


def describe(update: Mapping[str, Any]) -> tuple[str, int | None, str | None]:
    """(update kind, sender id, chat type) for drop logging. Never includes message text."""
    kind = next((k for k in update if k != "update_id"), "unknown")
    obj = _mapping(update.get(kind)) or {}
    sender = _mapping(obj.get("from")) or {}
    chat = _mapping(obj.get("chat")) or {}
    if not chat:
        msg = _mapping(obj.get("message")) or {}  # callback_query.message
        chat = _mapping(msg.get("chat")) or {}
    chat_type = chat.get("type")
    return kind, _int(sender.get("id")), chat_type if isinstance(chat_type, str) else None


class DropLog:
    """At most `per_minute` drop lines per minute; the rest are counted and summarised."""

    def __init__(self, per_minute: int = 10) -> None:
        self.per_minute = per_minute
        self._window = 0.0
        self._count = 0
        self._suppressed = 0

    def drop(self, update: Mapping[str, Any], *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if now - self._window >= 60:
            if self._suppressed:
                log.warning(
                    "telegram: %d more updates dropped (log rate-limited)", self._suppressed
                )
            self._window, self._count, self._suppressed = now, 0, 0
        if self._count >= self.per_minute:
            self._suppressed += 1
            return
        self._count += 1
        kind, sender, chat_type = describe(update)
        log.warning(
            "telegram: dropped %s update from sender_id=%s chat_type=%s", kind, sender, chat_type
        )
