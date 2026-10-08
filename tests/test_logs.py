"""M11 structured logging: JSON lines, extras, and secret redaction (S4)."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from sqlalchemy import Engine

from aether.jobs import JobResult, run_job
from aether.logs import MASK, make_handler, secret_values, setup_logging
from tests.conftest import make_settings

# Synthetic secrets: test values only, never real keys.
FAKE_KEY = "test-anthropic-key-0000000000"
FAKE_TOKEN = "123456:synthetic-bot-token-value"


def _capture(settings_kw: dict[str, object], tmp_path: Path) -> tuple[logging.Logger, io.StringIO]:
    s = make_settings(tmp_path / "x.db", **settings_kw)
    buf = io.StringIO()
    lg = logging.getLogger(f"aether.test.{len(settings_kw)}.{id(buf)}")
    lg.handlers[:] = [make_handler(s, buf)]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    return lg, buf


def test_json_lines_with_extras(tmp_path: Path) -> None:
    lg, buf = _capture({"log_format": "json"}, tmp_path)
    lg.info("job %s ok", "prices", extra={"job": "prices", "run_id": 7, "duration_ms": 12})
    rec = json.loads(buf.getvalue())
    assert rec["msg"] == "job prices ok" and rec["level"] == "INFO"
    assert (rec["job"], rec["run_id"], rec["duration_ms"]) == ("prices", 7, 12)
    assert rec["ts"].endswith("Z") and rec["logger"].startswith("aether.test")


def test_secrets_are_masked_in_message_args_extras_and_tracebacks(tmp_path: Path) -> None:
    lg, buf = _capture(
        {"log_format": "json", "anthropic_api_key": FAKE_KEY, "telegram_bot_token": FAKE_TOKEN},
        tmp_path,
    )
    lg.info(
        "calling with %s", FAKE_KEY, extra={"url": f"https://api.example.test/bot{FAKE_TOKEN}/x"}
    )
    try:
        raise RuntimeError(f"auth failed for {FAKE_KEY}")
    except RuntimeError:
        lg.exception("boom sk-ant-api03-abcdefghijklmnop")
    out = buf.getvalue()
    assert FAKE_KEY not in out and FAKE_TOKEN not in out and "sk-ant-api03" not in out
    lines = [json.loads(x) for x in out.splitlines()]
    assert lines[0]["msg"] == f"calling with {MASK}" and MASK in lines[0]["url"]
    assert "RuntimeError" in lines[1]["exc"] and MASK in lines[1]["exc"]


def test_text_format_and_short_values_not_masked(tmp_path: Path) -> None:
    lg, buf = _capture({"log_format": "text", "telegram_allowed_user_id": "42"}, tmp_path)
    lg.info("hello", extra={"job": "x"})
    assert buf.getvalue().rstrip().endswith("INFO " + lg.name + " hello job=x")
    s = make_settings(tmp_path / "y.db", csrf_secret="short")
    assert "short" not in secret_values(s)


def test_run_job_logs_status_and_duration(rw_engine: Engine, tmp_path: Path) -> None:
    buf = io.StringIO()
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    setup_logging(make_settings(tmp_path / "x.db", log_format="json"))
    root.handlers[0].setStream(buf)  # type: ignore[attr-defined]
    try:
        run_job(rw_engine, "synthetic", lambda: JobResult(rows_written=2, provider="p"))
    finally:
        root.handlers[:] = saved
        root.setLevel(level)
    recs = [json.loads(x) for x in buf.getvalue().splitlines() if '"job": "synthetic"' in x]
    assert [r["msg"] for r in recs] == ["job synthetic started", "job synthetic ok"]
    assert recs[1]["status"] == "ok" and recs[1]["rows"] == 2 and recs[1]["duration_ms"] >= 0
