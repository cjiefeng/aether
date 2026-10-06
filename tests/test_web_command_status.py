"""Issue #18: inline command status (queued → running → done/failed), dedupe, rate-limit copy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, update
from starlette.testclient import TestClient

from aether.db.engine import write_tx
from aether.db.models import commands
from aether.db.types import to_iso
from aether.jobs import process_commands
from aether.web import command_view
from tests.test_web_security import csrf_token


def _post(client: TestClient, path: str) -> object:
    return client.post(path, headers={"X-CSRF-Token": csrf_token(client)})


def _set(engine: Engine, cid: int, **values: object) -> None:
    with write_tx(engine) as conn:
        conn.execute(update(commands).where(commands.c.id == cid).values(**values))


def test_enqueue_returns_polling_status_not_a_bare_id(client: TestClient) -> None:
    r = _post(client, "/commands/refresh-prices")
    assert r.status_code == 202
    assert "Price refresh requested" in r.text
    assert "Queued command" not in r.text
    assert 'hx-get="/commands/1/status?shown=queued"' in r.text
    assert 'hx-trigger="every 2s"' in r.text


def test_status_is_204_while_unchanged_then_final_without_polling(
    client: TestClient, rw_engine: Engine
) -> None:
    _post(client, "/commands/refresh-prices")
    assert client.get("/commands/1/status?shown=queued").status_code == 204

    _set(rw_engine, 1, status="running")
    r = client.get("/commands/1/status?shown=queued")
    assert r.status_code == 200 and "Price refresh running" in r.text
    assert "shown=running" in r.text

    _set(rw_engine, 1, status="done", result=json.dumps({"ok": True, "rows": 14}))
    r = client.get("/commands/1/status?shown=running")
    assert "Prices updated, 14 rows written." in r.text
    assert "Reload page" in r.text
    assert "hx-get" not in r.text and "hx-trigger" not in r.text


def test_failed_and_soft_failures_are_alerts(client: TestClient, rw_engine: Engine) -> None:
    _post(client, "/commands/sync-holdings")
    _set(rw_engine, 1, status="done", result=json.dumps({"ok": False, "error": "sync failed"}))
    r = client.get("/commands/1/status")
    assert 'role="alert"' in r.text and "Tiger sync didn&#39;t complete." in r.text
    assert "sync failed" in r.text and 'href="/health"' in r.text


def test_busy_result_is_a_warning(client: TestClient, rw_engine: Engine) -> None:
    _post(client, "/commands/refresh-edgar")
    _set(rw_engine, 1, status="done", result=json.dumps({"ok": False, "busy": True}))
    r = client.get("/commands/1/status")
    assert "cmd-warning" in r.text and "already running" in r.text


def test_duplicate_click_reuses_the_active_command(client: TestClient, rw_engine: Engine) -> None:
    _post(client, "/commands/research-sweep")
    r = _post(client, "/commands/research-sweep")
    assert r.status_code == 200 and "Already in progress" in r.text
    assert "Command #1" in r.text
    _set(rw_engine, 1, status="done", result=json.dumps({"ok": True, "rows": 0}))
    assert _post(client, "/commands/research-sweep").status_code == 202  # finished: new one


def test_page_load_shows_in_progress_command(client: TestClient) -> None:
    _post(client, "/commands/refresh-edgar")
    r = client.get("/")
    assert "SEC filings refresh requested" in r.text
    assert "/commands/1/status" in r.text


def test_rate_limit_says_when_to_retry(client: TestClient) -> None:
    for _ in range(10):
        _post(client, "/commands/ping")
    r = _post(client, "/commands/ping")
    assert r.status_code == 429
    assert "Hourly limit of 10 requests reached. Try again at" in r.text
    assert 'role="alert"' in r.text


def test_invalid_input_is_an_escaped_alert(client: TestClient) -> None:
    r = client.post(
        "/commands/publish-targets",
        data={"trigger_event_id": "<b>x"},
        headers={"X-CSRF-Token": csrf_token(client)},
    )
    assert r.status_code == 400
    assert 'role="alert"' in r.text and "<b>" not in r.text


def test_unknown_command_is_404(client: TestClient) -> None:
    assert client.get("/commands/999/status").status_code == 404


def test_worker_marks_running_then_done(rw_engine: Engine) -> None:
    from aether.db.commands import enqueue_command

    cid = enqueue_command(rw_engine, "ping", {}, "test")
    seen: list[str] = []

    def handler(_args: dict[str, object]) -> dict[str, object]:
        with rw_engine.connect() as conn:
            seen.append(conn.execute(commands.select().where(commands.c.id == cid)).one().status)
        return {"pong": "x"}

    process_commands(rw_engine, handlers={"ping": handler})
    with rw_engine.connect() as conn:
        row = conn.execute(commands.select().where(commands.c.id == cid)).one()
    assert seen == ["running"] and row.status == "done"


def test_stale_states_stop_polling() -> None:
    now = datetime.now(UTC)

    class Row:
        id, kind, result = 7, "refresh_prices", None
        status = "pending"
        requested_at = to_iso(now - timedelta(minutes=6))

    st = command_view.status_for(Row(), now)
    assert st.state == "stale" and not st.polling and "Is the worker running?" in st.message
    Row.status = "running"
    Row.requested_at = to_iso(now - timedelta(hours=2))
    assert command_view.status_for(Row(), now).state == "stale"
