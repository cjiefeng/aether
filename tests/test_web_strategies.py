"""Strategies page + curves API, rendered from a synthetic backtest via the ro engine."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import Engine
from starlette.testclient import TestClient

from aether.config import load_strategies
from aether.portfolio.job import run_strategies
from aether.portfolio.view import BANNER
from aether.security.csrf import COOKIE_NAME
from aether.security.headers import CSP
from tests.conftest import CONFIG_DIR
from tests.portfolio_data import panel, seed_panel

CONFIG = load_strategies(CONFIG_DIR)


@pytest.fixture
def backtested(rw_engine: Engine) -> Engine:
    seed_panel(rw_engine, panel(late={"FAKE": 170}))
    run_strategies(rw_engine, CONFIG)
    return rw_engine


def test_empty_page_shows_banner(rw_engine: Engine, client: TestClient) -> None:
    r = client.get("/strategies")
    assert r.status_code == 200
    assert BANNER in r.text
    assert "No backtest results yet." in r.text
    assert "No successful backtest run yet." in r.text
    assert client.get("/api/strategies/curves?profile=safe").json()["equity"] == []


def test_page_renders_profiles_metrics_and_caveats(backtested: Engine, client: TestClient) -> None:
    r = client.get("/strategies")
    assert r.status_code == 200
    text = r.text
    assert text.count(BANNER) >= 2  # banner + footer
    assert "not financial advice" in text
    assert "FAKE has 90 sessions of history" in text
    assert "Risk-free rate is 0" in text
    for p in ("Safe", "Medium", "Aggressive"):
        assert f"<h2>{p}</h2>" in text
    assert text.count("Model strategy:") == 3
    assert "Current target weights" in text and "Full metrics (out-of-sample)" in text
    assert 'data-chart="strategy-equity"' in text


def test_no_qualifying_strategy_renders(rw_engine: Engine, client: TestClient) -> None:
    seed_panel(rw_engine, panel())
    safe = CONFIG.profiles["safe"].model_copy(update={"vol_limit_x": 0.01})
    run_strategies(
        rw_engine, CONFIG.model_copy(update={"profiles": {**CONFIG.profiles, "safe": safe}})
    )
    r = client.get("/strategies")
    assert "No qualifying strategy." in r.text
    assert "candidates break the profile&#39;s limits" in r.text
    assert r.text.count("Model strategy:") == 2
    assert client.get("/api/strategies/curves?profile=safe").json()["strategy_id"] is None


def test_curves_api(backtested: Engine, client: TestClient) -> None:
    body = client.get("/api/strategies/curves?profile=medium").json()
    assert body["strategy_id"].startswith("medium:")
    names = [s["name"] for s in body["equity"]]
    assert names == ["Recommended", "QTUM", "QQQ"]
    eq = body["equity"][0]["data"]
    assert eq[0][1] == 100.0
    dd = body["drawdown"][0]["data"]
    assert max(v for _, v in dd) <= 0 and len(dd) == len(eq)
    assert client.get("/api/strategies/curves?profile=yolo").status_code == 404


def test_no_inline_script_or_style(backtested: Engine, client: TestClient) -> None:
    r = client.get("/strategies")
    assert r.headers["content-security-policy"] == CSP
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text)
    assert "<style" not in r.text and " style=" not in r.text


def test_recompute_command_requires_csrf(rw_engine: Engine, client: TestClient) -> None:
    assert client.post("/commands/recompute-strategies").status_code == 403
    client.get("/strategies")
    token = client.cookies.get(COOKIE_NAME)
    r = client.post("/commands/recompute-strategies", headers={"X-CSRF-Token": token or ""})
    assert r.status_code == 202
