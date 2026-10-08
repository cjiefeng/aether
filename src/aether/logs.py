"""Logging setup for the worker and the dashboard (M11): `AETHER_LOG_FORMAT=json` writes one JSON
object per line (`ts`, `level`, `logger`, `msg`, extras such as `job`/`run_id`/`status`/
`duration_ms`, and `exc` for tracebacks); `text` keeps the classic format.

Every record passes `RedactFilter` first (S4: never log secrets): the values of the secret settings
present in this process, and anything shaped like an Anthropic key, are masked in the message,
the arguments and the traceback.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from aether.config import Settings

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
MASK = "[REDACTED]"
_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
MIN_SECRET_LEN = 8  # shorter values would mask ordinary words

# Fields every LogRecord has; anything else was passed via `extra=` and goes into the JSON line.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


def secret_values(settings: Settings) -> list[str]:
    out = []
    for name in (
        "anthropic_api_key",
        "massive_api_key",
        "telegram_bot_token",
        "dashboard_password_hash",
        "session_secret",
        "csrf_secret",
        "tiger_private_key",
        "tiger_account",
    ):
        v = getattr(settings, name, None)
        if v is not None:
            raw = v.get_secret_value()
            # A PEM key may be logged line by line; mask each long line as well as the whole.
            out += [raw, *(ln.strip() for ln in raw.splitlines())]
    return sorted({s for s in out if len(s) >= MIN_SECRET_LEN}, key=len, reverse=True)


class RedactFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if len(s) >= MIN_SECRET_LEN]

    def scrub(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, MASK)
        return _KEY_RE.sub(MASK, text)

    def filter(self, record: logging.LogRecord) -> bool:
        # Render once, scrub, and freeze: handlers then never see the raw arguments.
        record.msg = self.scrub(record.getMessage())
        record.args = None
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = self.scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        for key, value in list(vars(record).items()):
            if key not in _STANDARD and isinstance(value, str):
                setattr(record, key, self.scrub(value))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")[
                :-6
            ]
            + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                out[key] = value if isinstance(value, int | float | bool | None) else str(value)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            out["exc"] = record.exc_text
        return json.dumps(out, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = {
            k: v for k, v in vars(record).items() if k not in _STANDARD and not k.startswith("_")
        }
        if extras:
            line += " " + " ".join(f"{k}={v}" for k, v in sorted(extras.items()))
        return line


def make_handler(settings: Settings, stream: Any = None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(RedactFilter(secret_values(settings)))
    handler.setFormatter(
        JsonFormatter() if settings.log_format == "json" else _TextFormatter(TEXT_FORMAT)
    )
    return handler


def setup_logging(settings: Settings) -> None:
    """Root logger → one redacting handler. uvicorn's loggers propagate to it."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(make_handler(settings))
    root.setLevel(settings.log_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
