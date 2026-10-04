"""Telegram Bot API client, owner-only (spec S7). Checked against Bot API 10.3 (2026-08-24).

- **Fail closed:** `TelegramConfig.from_settings` returns no config (module disabled, alerts stay
  on the dashboard) when the token is set but `TELEGRAM_ALLOWED_USER_ID` is missing or not a
  positive integer, or when `TELEGRAM_CHAT_ID` differs from it.
- **Outbound:** `send_message` takes no chat argument; it only ever sends to the configured chat,
  and only after `verify()` has seen `getChat` return `type == "private"` and `id == owner`.
- **Inbound:** long polling (`getUpdates`) only, after `deleteWebhook`. Every update passes
  `telegram_guard.is_owner` first; groups and channels are left with `leaveChat`.
- **Secrets:** the bot token is part of every request URL, so the `httpx` logger is held at
  WARNING and every error is re-raised as `TelegramError` with the token scrubbed (and `from
  None`, so the original exception and its URL never reach a traceback or `job_runs.error`).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from aether.alerts import telegram_guard
from aether.config import Settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_TEXT = 4096
MAX_RETRY_AFTER_S = 30
VERIFY_TTL = timedelta(hours=6)
POLL_TIMEOUT_S = 20
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
_ID_RE = re.compile(r"^[1-9]\d{0,19}$")


class TelegramError(Exception):
    """A Bot API or transport failure. The message never contains the token."""


def redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "<redacted>")
    return _TOKEN_RE.sub("<redacted>", text)


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    owner_id: int
    chat_id: int

    def __repr__(self) -> str:  # never print the token
        return f"TelegramConfig(owner_id={self.owner_id}, chat_id={self.chat_id})"

    @classmethod
    def from_settings(cls, settings: Settings) -> tuple[TelegramConfig | None, str | None]:
        """(config, None) when enabled, else (None, reason). Logs an error on a bad config."""
        if settings.telegram_bot_token is None:
            return None, "TELEGRAM_BOT_TOKEN not set"
        raw_owner = (settings.telegram_allowed_user_id or "").strip()
        if not _ID_RE.fullmatch(raw_owner):
            reason = "TELEGRAM_ALLOWED_USER_ID missing or not a numeric user ID"
            log.error("telegram disabled (fail closed): %s", reason)
            return None, reason
        owner = int(raw_owner)
        raw_chat = (settings.telegram_chat_id or "").strip()
        if raw_chat and (not _ID_RE.fullmatch(raw_chat) or int(raw_chat) != owner):
            reason = "TELEGRAM_CHAT_ID must equal TELEGRAM_ALLOWED_USER_ID (owner's private chat)"
            log.error("telegram disabled (fail closed): %s", reason)
            return None, reason
        return cls(settings.telegram_bot_token.get_secret_value(), owner, owner), None


class TelegramBot:
    """Thin typed wrapper over the Bot API methods Aether uses."""

    def __init__(
        self,
        config: TelegramConfig,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.config = config
        self._client = client or httpx.Client(timeout=POLL_TIMEOUT_S + 15)
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, params: Mapping[str, Any], *, retry: bool = True) -> Any:
        url = f"{API}/bot{self.config.token}/{method}"
        try:
            resp = self._client.post(url, json=dict(params))
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            msg = redact(f"{method}: {type(exc).__name__}: {exc}", self.config.token)
            raise TelegramError(msg) from None
        if not isinstance(body, dict):
            raise TelegramError(f"{method}: HTTP {resp.status_code}, unexpected body")
        if body.get("ok") is True:
            return body.get("result")
        code = body.get("error_code")
        params_ = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
        retry_after = params_.get("retry_after") if params_ else None
        if code == 429 and retry and isinstance(retry_after, int):
            self._sleep(min(max(retry_after, 1), MAX_RETRY_AFTER_S))
            return self._call(method, params, retry=False)
        desc = str(body.get("description", ""))[:200]
        raise TelegramError(redact(f"{method}: error {code}: {desc}", self.config.token))

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", {"drop_pending_updates": False})

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        result = self._call("getChat", {"chat_id": chat_id})
        if not isinstance(result, dict):
            raise TelegramError("getChat: unexpected result")
        return result

    def send_message(self, text: str) -> None:
        """Plain text (no parse_mode) to the configured owner chat only."""
        self._call(
            "sendMessage",
            {
                "chat_id": self.config.chat_id,
                "text": text[:MAX_TEXT],
                "link_preview_options": {"is_disabled": True},
            },
        )

    def leave_chat(self, chat_id: int) -> None:
        self._call("leaveChat", {"chat_id": chat_id})

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", params)
        if not isinstance(result, list):
            raise TelegramError("getUpdates: unexpected result")
        return [u for u in result if isinstance(u, dict)]


class TelegramService:
    """Per-process Telegram state: chat verification, the getUpdates offset, the drop log."""

    def __init__(self, bot: TelegramBot) -> None:
        self.bot = bot
        self.owner_id = bot.config.owner_id
        # Set when getChat shows the configured chat isn't the owner's private chat. Sending
        # then stays off for the life of the process.
        self.blocked: str | None = None
        self._verified_at: datetime | None = None
        self._webhook_deleted = False
        self._offset: int | None = None
        self._drops = telegram_guard.DropLog()

    def _ensure_no_webhook(self) -> None:
        if not self._webhook_deleted:
            self.bot.delete_webhook()
            self._webhook_deleted = True

    def verify(self, now: datetime | None = None) -> bool:
        """True if sending is allowed. Raises TelegramError on transport failure (transient)."""
        if self.blocked:
            return False
        now = now or datetime.now(UTC)
        if self._verified_at and now - self._verified_at < VERIFY_TTL:
            return True
        self._ensure_no_webhook()
        chat = self.bot.get_chat(self.bot.config.chat_id)
        if chat.get("type") != "private" or chat.get("id") != self.owner_id:
            self.blocked = (
                f"getChat returned type={chat.get('type')!r}; the configured chat is not the "
                "owner's private chat, so nothing is sent"
            )
            log.error("telegram: %s", self.blocked)
            return False
        self._verified_at = now
        return True

    def handle_update(self, update: Mapping[str, Any]) -> None:
        for chat_id in sorted(telegram_guard.chats_to_leave(update)):
            try:
                self.bot.leave_chat(chat_id)
                log.warning("telegram: left shared chat (id=%s)", chat_id)
            except TelegramError as exc:
                log.warning("telegram: leaveChat failed: %s", exc)
        if telegram_guard.is_owner(update, self.owner_id):
            # The MVP has no commands. Future handlers go here, behind this guard, and anything
            # that writes or spends goes through the `commands` table.
            log.info("telegram: owner message received (no commands in the MVP)")
            return
        self._drops.drop(update)

    def poll_inbound(self, timeout: int = POLL_TIMEOUT_S) -> int:
        """One long-poll round. Returns the number of updates handled."""
        self._ensure_no_webhook()
        updates = self.bot.get_updates(self._offset, timeout)
        for update in updates:
            uid = update.get("update_id")
            if isinstance(uid, int):
                self._offset = max(self._offset or 0, uid + 1)
            self.handle_update(update)
        return len(updates)
