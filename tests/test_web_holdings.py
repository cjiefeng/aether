"""Holdings page and Overview drift card, rendered from synthetic data via the ro engine."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from sqlalchemy import Engine
from starlette.testclient import TestClient

from aether.config import load_strategies
from aether.portfolio.holdings import (
    HoldingsUpdate,
    PositionIn,
    SettingsUpdate,
    apply_holdings_update,
    apply_settings_update,
)
from aether.portfolio.publish import publish_targets, run_rebalance
from aether.portfolio.view import BANNER
from aether.security.headers import CSP
from tests.conftest import CONFIG_DIR
from tests.holdings_data import add_event, add_filing, fake_run, seed_prices, seed_universe

CONFIG = load_strategies(CONFIG_DIR)
D = Decimal
A = {"QTUM": 0.75, "ACME": 0.10, "DEMO": 0.10, "EXMP": 0.05}


def no_inline(text: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text)
    assert "<style" not in text and " style=" not in text


def test_empty_page(rw_engine: Engine, client: TestClient) -> None:
    seed_universe(rw_engine)
    r = client.get("/holdings")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == CSP
    assert r.text.count(BANNER) >= 2 and "not financial advice" in r.text
    assert "No holdings saved" in r.text
    assert 'name="shares_ACME"' in r.text and 'name="cash"' in r.text
    assert '<option value="safe" selected>' in r.text
    assert "Sync from Tiger" not in r.text
    no_inline(r.text)
    assert "Position drift" not in client.get("/").text


def test_plan_renders_with_publish_dates_and_drift_card(
    rw_engine: Engine, client: TestClient
) -> None:
    days = seed_prices(rw_engine)
    apply_holdings_update(
        rw_engine,
        HoldingsUpdate(
            positions=(
                PositionIn(symbol="QTUM", shares=D(100)),
                PositionIn(symbol="FAKE", shares=D(40)),
            ),
            cash=D(3000),
        ),
    )
    fake_run(rw_engine, days[-1], {"safe": A})
    run_rebalance(rw_engine, CONFIG, today=date(2026, 9, 1))
    text = client.get("/holdings").text
    assert "Published targets: Safe" in text
    assert "Targets published 2026-09-01" in text and "next publish 2026-10-01" in text
    assert "Rebalance plan: Safe" in text
    assert re.search(r"Sell \d+ FAKE", text)  # zero-target holding sold
    assert "Publish targets now" in text
    no_inline(text)
    assert "Position drift vs the safe profile" in client.get("/").text


def test_overlay_chain_and_off_cycle_event_on_page(rw_engine: Engine, client: TestClient) -> None:
    days = seed_prices(rw_engine)
    apply_holdings_update(
        rw_engine, HoldingsUpdate(positions=(PositionIn(symbol="ACME", shares=D(10)),), cash=D(500))
    )
    _, gc = add_filing(
        rw_engine,
        "ACME",
        "10-Q",
        "2026-08-20",
        parsed={"going_concern": True},
        event=("RISK", "going_concern", 5, "edgar_going_concern_text"),
    )
    fake_run(rw_engine, days[-1], {"safe": A})
    publish_targets(rw_engine, CONFIG, today=date(2026, 9, 1))
    text = client.get("/holdings").text
    assert f"going concern (event #{gc})" in text
    assert "https://example.test/filing/" in text  # evidence link via extlink
    eid = add_event(rw_engine, "DEMO", "2099-01-01T00:00:00Z", materiality=5)
    text = client.get("/holdings").text
    assert f"#{eid} DEMO" in text
    assert f'name="trigger_event_id" value="{eid}"' in text


def test_publish_command(rw_engine: Engine, client: TestClient) -> None:
    from aether.security.csrf import COOKIE_NAME

    seed_universe(rw_engine)
    assert client.post("/commands/publish-targets").status_code == 403
    client.get("/holdings")
    h = {"X-CSRF-Token": client.cookies.get(COOKIE_NAME) or ""}
    assert (
        client.post(
            "/commands/publish-targets", data={"trigger_event_id": "x1"}, headers=h
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/commands/publish-targets", data={"trigger_event_id": "12"}, headers=h
        ).status_code
        == 202
    )


def test_tiger_mode_page(rw_engine: Engine, client: TestClient) -> None:
    seed_universe(rw_engine)
    apply_settings_update(rw_engine, SettingsUpdate(holdings_source="tiger"))
    text = client.get("/holdings").text
    assert "Sync from Tiger" in text
    assert "Tiger holdings never synced" in text
    assert 'name="shares_QTUM"' not in text  # universe rows are read-only
    assert 'name="cash"' in text


def test_sync_command_requires_csrf(rw_engine: Engine, client: TestClient) -> None:
    assert client.post("/commands/sync-holdings").status_code == 403


# --------------------------------------------------------------------------- performance (#24)


def _numbers(x: object) -> set[float]:
    if isinstance(x, bool) or x is None or isinstance(x, str):
        return set()
    if isinstance(x, int | float):
        return {float(x)}
    if isinstance(x, dict):
        return set().union(*(_numbers(v) for v in x.values()))
    if isinstance(x, list):
        return set().union(*(_numbers(v) for v in x))
    return set()


def test_performance_card_empty_state(rw_engine: Engine, client: TestClient) -> None:
    seed_universe(rw_engine)
    r = client.get("/holdings")
    assert "Add holdings to see performance vs QTUM and QQQ." in r.text
    assert 'data-chart="performance"' not in r.text


def test_performance_endpoint_carries_no_position_sizes(
    rw_engine: Engine, client: TestClient
) -> None:
    seed_prices(rw_engine)
    apply_holdings_update(
        rw_engine,
        HoldingsUpdate(
            positions=(
                PositionIn(symbol="QTUM", shares=D("137.25"), cost_basis=D("61.17")),
                PositionIn(symbol="ACME", shares=D("43"), cost_basis=D("9.83")),
            ),
            cash=D("4321.09"),
        ),
    )
    page = client.get("/holdings")
    assert 'data-chart="performance"' in page.text and "Since tracking" in page.text
    no_inline(page.text)
    for mode in ("actual", "current"):
        for rng in ("1m", "1y", "since"):
            r = client.get(f"/api/holdings/performance?range={rng}&mode={mode}")
            assert r.status_code == 200
            assert r.headers["cache-control"] == "no-store"
            body = r.json()
            assert set(body) == {
                "range",
                "mode",
                "start",
                "end",
                "tracking_started",
                "series",
                "markers",
                "stats",
                "stale_since",
                "enough",
            }
            for word in ("shares", "cash", "cost", "value", "account"):
                assert word not in r.text, word
            assert not _numbers(body) & {137.25, 43.0, 4321.09, 61.17, 9.83}
    hyp = client.get("/api/holdings/performance?range=1y&mode=current").json()
    assert hyp["enough"] and [s["name"] for s in hyp["series"]][1:] == ["QTUM", "QQQ"]
    assert client.get("/api/holdings/performance?range=5y").status_code == 400
    assert client.get("/api/holdings/performance?mode=x").status_code == 400
