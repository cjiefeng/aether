"""Facts and Alerts pages, and the test-alert command (read-only pages; CSRF'd command)."""

from __future__ import annotations

import re

from sqlalchemy import Engine, insert, select
from starlette.testclient import TestClient

from aether.db.engine import write_tx
from aether.db.models import alerts, commands, job_runs
from aether.facts import load_facts, sync_facts
from aether.security.csrf import COOKIE_NAME
from tests.conftest import CONFIG_DIR


def _no_inline(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "style=" not in html


def test_facts_page_shows_registry_with_badges(rw_engine: Engine, client: TestClient) -> None:
    facts = load_facts(CONFIG_DIR)
    with write_tx(rw_engine) as conn:
        sync_facts(conn, facts)
    r = client.get("/facts")
    assert r.status_code == 200
    for f in facts:
        assert f'id="{f.id}"' in r.text
    assert "badge fact-" in r.text
    assert 'rel="noopener noreferrer nofollow"' in r.text  # sources via extlink
    # Open questions synced to SQLite (0004 column) render.
    with_q = [f for f in facts if f.open_question]
    assert with_q and "Open question" in r.text
    _no_inline(r.text)


def test_facts_page_empty(client: TestClient) -> None:
    r = client.get("/facts")
    assert r.status_code == 200 and "No facts synced yet" in r.text


def test_alerts_page_and_overview_card(rw_engine: Engine, client: TestClient) -> None:
    assert "hasn't run yet" in client.get("/alerts").text
    with write_tx(rw_engine) as conn:
        conn.execute(
            insert(alerts).values(
                kind="risk_event",
                channel="dashboard",
                status="dashboard_only",
                text="RISK · ACME · dilution (materiality 3/5)\n<b>ACME S-3</b>",
                created_at="2026-03-10T12:00:00Z",
                dedupe_key="risk_event:1",
            )
        )
        conn.execute(
            insert(job_runs).values(
                job="alerts",
                started_at="2026-03-10T12:00:00Z",
                finished_at="2026-03-10T12:00:01Z",
                status="ok",
                provider="dashboard",
                error="TELEGRAM_BOT_TOKEN not set",
            )
        )
    r = client.get("/alerts")
    assert r.status_code == 200
    assert "Dashboard only" in r.text and "TELEGRAM_BOT_TOKEN not set" in r.text
    assert "&lt;b&gt;ACME S-3&lt;/b&gt;" in r.text  # autoescaped
    _no_inline(r.text)
    overview = client.get("/").text
    assert "Recent alerts" in overview and "dashboard only" in overview


def test_test_alert_command_requires_csrf(rw_engine: Engine, client: TestClient) -> None:
    assert client.post("/commands/test-alert").status_code == 403
    client.get("/alerts")
    token = client.cookies.get(COOKIE_NAME)
    r = client.post("/commands/test-alert", headers={"X-CSRF-Token": token or ""})
    assert r.status_code == 202
    with rw_engine.connect() as conn:
        assert conn.execute(select(commands.c.kind)).scalars().all() == ["test_alert"]


def test_alerts_page_lists_merged_digested_and_dashboard_only_rows(
    rw_engine: Engine, client: TestClient
) -> None:
    """M13: every alert stays on /alerts; rows of one event link to each other."""
    from tests.conftest import seed_tickers
    from tests.holdings_data import add_event

    seed_tickers(rw_engine, [("ACME", "pure_play")])
    eid = add_event(rw_engine, "ACME", "2026-03-10T10:00:00Z", 4, "RISK", "dilution")
    rows = [
        ("risk_event", "sent", "immediate", eid, "RISK · ACME · dilution · escalated"),
        ("escalation", "dashboard_only", "merged", eid, "RISK · ACME · escalated"),
        ("escalation_result", "dashboard_only", "dashboard_only", eid, "Escalation result"),
        ("insider_cluster", "digested", "digest", None, "RISK · ACME · insider selling cluster"),
        ("digest", "sent", "immediate", None, "Daily digest · 2026-03-10 · 1 alert(s)"),
    ]
    with write_tx(rw_engine) as conn:
        for i, (kind, status, delivery, event_id, text) in enumerate(rows):
            conn.execute(
                insert(alerts).values(
                    kind=kind,
                    channel="telegram",
                    status=status,
                    delivery=delivery,
                    event_id=event_id,
                    text=text,
                    created_at="2026-03-10T12:00:00Z",
                    dedupe_key=f"k{i}",
                )
            )
    t = client.get("/alerts").text
    for *_, text in rows:
        assert text in t
    assert "merged into the event&#39;s message" in t or "merged into the event's message" in t
    assert "dashboard only (no change)" in t and "daily digest" in t
    assert 'href="/alerts#alert-1"' in t and 'href="/alerts#alert-3"' in t
    assert f'href="/feed?event={eid}"' in t
    assert "digest at 08:00 SGT" in t
    _no_inline(t)
