"""Issue #50: a research event dated only by its retrieval time is shown, but never drives a
decision: no removal trigger, escalation or off-cycle review. Synthetic ACME events only."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, update

from aether.alerts.candidates import off_cycle_reviews
from aether.config import RemovalRule, load_alerts_config, load_llm_config, load_strategies
from aether.db.engine import write_tx
from aether.db.models import events
from aether.escalate.select import candidates as escalation_candidates
from aether.portfolio.holdings_view import off_cycle_events
from aether.universe.slots import qualifying_triggers
from tests.conftest import CONFIG_DIR, seed_tickers
from tests.holdings_data import add_event

NOW = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


@pytest.fixture
def pair(rw_engine: Engine) -> tuple[Engine, int, int]:
    """Two RISK going-concern events (materiality 5, T1) for ACME: one with a real date, one
    whose date is only its retrieval time."""
    seed_tickers(rw_engine, [("ACME", "pure_play")])
    dated = add_event(rw_engine, "ACME", "2026-10-31T14:00:00Z")
    retrieved = add_event(rw_engine, "ACME", "2026-10-31T15:00:00Z")
    with write_tx(rw_engine) as conn:
        conn.execute(
            update(events)
            .where(events.c.id == dated)
            .values(raw=json.dumps({"research": "sweep", "date_source": "page_age"}))
        )
        conn.execute(
            update(events)
            .where(events.c.id == retrieved)
            .values(raw=json.dumps({"research": "sweep", "date_source": "retrieved"}))
        )
    return rw_engine, dated, retrieved


def test_retrieved_event_is_not_a_removal_trigger(pair: tuple[Engine, int, int]) -> None:
    engine, dated, retrieved = pair
    out = qualifying_triggers(
        engine,
        ["ACME"],
        date(2026, 10, 1),
        date(2026, 11, 1),
        load_strategies(CONFIG_DIR).overlay,
        RemovalRule(),
    )
    texts = [t.text for t in out["ACME"]]
    assert len(texts) == 1 and f"event #{dated} " in texts[0]
    assert not any(f"event #{retrieved} " in t for t in texts)


def test_retrieved_event_is_not_escalated(pair: tuple[Engine, int, int]) -> None:
    engine, dated, _retrieved = pair
    got = escalation_candidates(
        engine, load_llm_config(CONFIG_DIR).escalation, ["ACME"], NOW, lookback_days=3
    )
    assert {c.event_id for c in got} == {dated}


def test_retrieved_event_suggests_no_off_cycle_review(pair: tuple[Engine, int, int]) -> None:
    engine, dated, _retrieved = pair
    with engine.connect() as conn:
        alerts = off_cycle_reviews(conn, load_alerts_config(CONFIG_DIR), NOW, min_materiality=4)
    assert [a.dedupe_key for a in alerts] == [f"off_cycle:{dated}"]
    assert [e["id"] for e in off_cycle_events(engine, "2026-10-01")] == [dated]


def test_events_without_a_date_source_still_count(rw_engine: Engine) -> None:
    """EDGAR, RSS and manual events have no `date_source`: they're dated, as before."""
    seed_tickers(rw_engine, [("ACME", "pure_play")])
    eid = add_event(rw_engine, "ACME", "2026-10-31T14:00:00Z")  # raw = "{}"
    assert [e["id"] for e in off_cycle_events(rw_engine, "2026-10-01")] == [eid]
