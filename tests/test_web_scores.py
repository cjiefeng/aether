"""M9 dashboard: scorecard, valuation and reaction cards on the ticker page, the theme card and
score column on the Overview, the Calibration page and the reactions API. Synthetic data only."""

from __future__ import annotations

import re
from datetime import date

import numpy as np
from sqlalchemy import Engine
from starlette.testclient import TestClient

from aether.config import load_rubric, load_weights
from aether.score.calibration import run_calibration
from aether.score.reaction import run_reactions
from aether.score.scorecard import run_scorecards
from aether.score.theme import run_theme
from tests.conftest import CONFIG_DIR
from tests.fundamentals_data import CASH, COMMON, add_facts, cash_flow_history, fact
from tests.holdings_data import add_event
from tests.test_reactions import closes_from, market, seed

W = load_weights(CONFIG_DIR)


def no_inline(body: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", body)
    assert "<style" not in body and " style=" not in body


def populate(engine: Engine) -> int:
    days, rb, rs = market(300, date(2026, 6, 30))
    rng = np.random.default_rng(3)
    rs = rs.copy()
    rs[250] += 0.10
    seed(
        engine,
        days,
        {
            "QTUM": closes_from(rb),
            "QQQ": closes_from(rb * 0.8 + rng.normal(0, 0.005, len(rb))),
            "SOXX": closes_from(rb * 1.2 + rng.normal(0, 0.01, len(rb))),
            "ACME": closes_from(rs),
            "DEMO": closes_from(rng.normal(0.001, 0.03, len(rb))),
        },
    )
    add_facts(
        engine,
        [
            *cash_flow_history("ACME", -25),
            fact("ACME", CASH, "2026-03-31", 300, filed="2026-05-01"),
            fact("ACME", COMMON, "2026-04-30", 1000, filed="2026-05-01"),
        ],
    )
    eid = add_event(engine, "ACME", f"{days[250]}T14:00:00Z", 4, "SIGNAL", "contract_with_value")
    run_reactions(engine, W.reactions)
    run_theme(engine)
    run_scorecards(engine, W, load_rubric(CONFIG_DIR))
    run_calibration(engine, W.calibration, date(2026, 7, 5))
    return eid


def test_ticker_page_scorecard_valuation_and_reactions(
    rw_engine: Engine, client: TestClient
) -> None:
    populate(rw_engine)
    r = client.get("/t/ACME")
    assert r.status_code == 200
    body = r.text
    no_inline(body)
    assert 'id="scorecard"' in body and "Signal momentum" in body
    assert "Valuation and dilution (SEC XBRL)" in body and "Cash runway" in body
    assert "not tagged in XBRL (not estimated)" in body
    assert "Market reactions to events" in body and "Synthetic contract_with_value event" in body
    # add_event gives direction -1 and the stock jumped: the market disagreed.
    assert "market disagreed" in body
    assert 'data-markers="/api/reactions/ACME"' in body
    m = client.get("/api/reactions/ACME").json()["markers"]
    assert len(m) == 1 and m[0]["cls"] == "SIGNAL" and m[0]["z5"] is not None


def test_overview_theme_card_and_score_column(rw_engine: Engine, client: TestClient) -> None:
    populate(rw_engine)
    body = client.get("/").text
    no_inline(body)
    assert "quantum-sleeve view" in body and "partial R²" in body
    assert 'href="/t/ACME#scorecard"' in body


def test_calibration_page(rw_engine: Engine, client: TestClient) -> None:
    body = client.get("/calibration").text
    no_inline(body)
    assert "No calibration report yet" in body
    populate(rw_engine)
    body = client.get("/calibration").text
    no_inline(body)
    assert "Report as of 2026-07-05" in body
    assert "not as proof" in body
    assert "Implied vs realized move" in body
    assert 'href="/calibration" aria-current="page"' in body


def test_benchmark_ticker_page_has_no_score_cards(rw_engine: Engine, client: TestClient) -> None:
    populate(rw_engine)
    body = client.get("/t/QQQ").text
    assert 'id="scorecard"' not in body and "data-markers" not in body
