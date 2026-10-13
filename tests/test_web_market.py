"""Overview + ticker pages and chart JSON, rendered from synthetic prices via the ro engine."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import Engine
from starlette.testclient import TestClient

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import prices_daily, qtum_holdings
from aether.db.types import utcnow_iso
from aether.market import BASKET
from aether.providers.prices import US_EASTERN
from aether.runs import JobResult, run_job
from aether.security.headers import CSP
from tests.conftest import seed_tickers

SYMS = [
    ("QTUM", "etf"),
    ("ACME", "pure_play"),
    ("EXMP", "pure_play"),
    ("QQQ", "benchmark"),
    ("SOXX", "benchmark"),
]


def _seed_prices(engine: Engine, days: int = 60) -> None:
    today = datetime.now(US_EASTERN).date()
    rows = []
    for i, (sym, _) in enumerate(SYMS):
        for k in range(days):
            d = today - timedelta(days=days - 1 - k)
            c = 10.0 + i + k * 0.1
            rows.append(
                {
                    "symbol": sym,
                    "d": d.isoformat(),
                    "o": c,
                    "h": c,
                    "l": c,
                    "c": c,
                    "volume": 100,
                    "provider": "synthetic",
                    "fetched_at": utcnow_iso(),
                }
            )
    with write_tx(engine) as conn:
        upsert(conn, prices_daily, rows, key_cols=["symbol", "d"])


def _seed_holdings(engine: Engine, snap: date) -> None:
    rows = [
        {
            "snapshot_date": snap.isoformat(),
            "holding_symbol": s,
            "name": s,
            "cusip": "X",
            "weight": w,
            "shares": 1,
            "fetched_at": utcnow_iso(),
        }
        for s, w in (("ACME", 1.25), ("OTHER", 98.75))
    ]
    with write_tx(engine) as conn:
        upsert(conn, qtum_holdings, rows, key_cols=["snapshot_date", "holding_symbol"])


@pytest.fixture
def seeded(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, SYMS)
    return rw_engine


def test_overview_without_data_shows_stale_banners(seeded: Engine, client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "No successful price run yet" in r.text
    assert "No QTUM holdings snapshot yet" in r.text
    assert "not financial advice" in r.text


def test_overview_with_data(seeded: Engine, client: TestClient) -> None:
    _seed_prices(seeded)
    _seed_holdings(seeded, datetime.now(US_EASTERN).date())
    run_job(seeded, "prices", lambda: JobResult(rows_written=1, provider="synthetic"))
    run_job(seeded, "edgar", lambda: JobResult(rows_written=0, provider="sec"))
    r = client.get("/")
    assert r.status_code == 200
    assert "stale" not in r.text.lower()
    assert 'href="/t/ACME"' in r.text
    assert "1.25%" in r.text and "not held" in r.text  # EXMP absent from the snapshot
    assert 'data-chart="overview"' in r.text


def test_old_price_run_shows_stale_since(seeded: Engine, client: TestClient) -> None:
    from aether.db.models import job_runs

    with write_tx(seeded) as conn:
        conn.execute(
            job_runs.insert().values(
                job="prices",
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:01:00Z",
                status="ok",
            )
        )
    assert "Prices stale since 2026-01-01T00:01:00Z" in client.get("/").text


def test_overview_json(seeded: Engine, client: TestClient) -> None:
    _seed_prices(seeded)
    body = client.get("/api/prices/overview?range=1m").json()
    names = {s["name"]: s["data"] for s in body["series"]}
    assert set(names) == {"QTUM", "SOXX", "QQQ", BASKET}
    for data in names.values():
        assert data[0][1] == 100.0 and len(data) <= 31
    assert client.get("/api/prices/overview?range=99y").status_code == 400


def test_ticker_page_and_json(seeded: Engine, client: TestClient) -> None:
    _seed_prices(seeded, days=5)
    r = client.get("/t/ACME")
    assert r.status_code == 200 and 'data-src="/api/prices/ACME"' in r.text
    bars = client.get("/api/prices/ACME").json()["bars"]
    assert len(bars) == 5 and set(bars[0]) == {"d", "o", "h", "l", "c", "v", "p"}


@pytest.mark.parametrize("path", ["/t/NOPE", "/t/bad%20sym", "/api/prices/NOPE", "/t/acme"])
def test_unknown_symbols_404(seeded: Engine, client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/", "/t/ACME", "/health"])
def test_pages_have_no_inline_script_or_style(
    seeded: Engine, client: TestClient, path: str
) -> None:
    r = client.get(path)
    assert r.headers["content-security-policy"] == CSP
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text)  # every <script> has src=
    assert "<style" not in r.text and " style=" not in r.text


def test_refresh_prices_command_requires_csrf(seeded: Engine, client: TestClient) -> None:
    assert client.post("/commands/refresh-prices").status_code == 403
    client.get("/")
    from aether.security.csrf import COOKIE_NAME

    token = client.cookies.get(COOKIE_NAME)
    r = client.post("/commands/refresh-prices", headers={"X-CSRF-Token": token or ""})
    assert r.status_code == 202


# --------------------------------------------------------------------------- issue #39

MIXED = [
    ("QTUM", "etf"),
    ("ACME", "pure_play"),
    ("BETA", "pure_play"),
    ("ZETA", "pure_play"),  # not in the snapshot
    ("ADJA", "adjacent"),
    ("ADJB", "adjacent"),  # not in the snapshot
    ("CTXA", "context"),
    ("CTXB", "context"),
    ("QQQ", "benchmark"),
    ("SOXX", "benchmark"),
]
MIXED_WEIGHTS = {
    "ACME": 0.95,
    "BETA": 0.72,
    "ADJA": 0.30,
    "CTXA": 1.55,
    "CTXB": 1.13,
    "QQQ": 2.0,
    "SOXX": 1.0,
    "OTHER": 91.35,  # funds and non-watchlist names never count
}


def _seed_mixed(engine: Engine, snap: date) -> None:
    seed_tickers(engine, MIXED)
    rows = [
        {
            "snapshot_date": snap.isoformat(),
            "holding_symbol": s,
            "name": s,
            "cusip": "X",
            "weight": w,
            "shares": 1,
            "fetched_at": utcnow_iso(),
        }
        for s, w in MIXED_WEIGHTS.items()
    ]
    with write_tx(engine) as conn:
        upsert(conn, qtum_holdings, rows, key_cols=["snapshot_date", "holding_symbol"])


def test_watchlist_weight_groups_subtotals_and_not_held(rw_engine: Engine) -> None:
    from aether import market

    today = date(2026, 10, 13)
    _seed_mixed(rw_engine, today)
    watch = market.watch_symbols(market.load_tickers(rw_engine))
    assert {s for s, _ in watch} == {"ACME", "BETA", "ZETA", "ADJA", "ADJB", "CTXA", "CTXB"}
    h = market.holdings_view(rw_engine, watch, today)
    groups = {g.type: g for g in h.groups}
    assert [g.label for g in h.groups] == ["Pure-plays", "Adjacent", "Context"]
    assert groups["pure_play"].symbols == ("ACME", "BETA", "ZETA")  # held by weight, then not held
    assert groups["adjacent"].symbols == ("ADJA", "ADJB")
    assert groups["context"].symbols == ("CTXA", "CTXB")
    assert h.subtotal("pure_play") == pytest.approx(1.67)
    assert h.subtotal("adjacent") == pytest.approx(0.30)
    assert h.subtotal("context") == pytest.approx(2.68)
    assert h.combined == pytest.approx(4.65)
    assert "ZETA" not in h.weights and "ADJB" not in h.weights
    assert not {"QTUM", "QQQ", "SOXX", "OTHER"} & set(h.weights)


def test_overview_card_lists_every_watchlist_name(rw_engine: Engine, client: TestClient) -> None:
    _seed_mixed(rw_engine, datetime.now(US_EASTERN).date())
    text = client.get("/").text
    card = text.split("<h2>Watchlist weight in QTUM</h2>", 1)[1].split("</section>", 1)[0]
    assert "Pure-plays combined: <strong>1.67%</strong>" in card
    assert "all watchlist names: <strong>4.65%</strong>" in card
    for label, sub in (("Pure-plays", "1.67%"), ("Adjacent", "0.30%"), ("Context", "2.68%")):
        assert f'{label} subtotal</th><td class="num"><strong>{sub}</strong>' in card
    assert 'All watchlist names</th><td class="num"><strong>4.65%</strong>' in card
    listed = re.findall(r'<td><a href="/t/([A-Z]+)">', card)
    assert listed == ["ACME", "BETA", "ZETA", "ADJA", "ADJB", "CTXA", "CTXB"]
    assert card.count("not held") == 2
    # The basket chart's member list stays pure-play only.
    basket_note = text.split('data-chart="overview"', 1)[1].split("</p>", 1)[0]
    assert (
        "ACME, BETA, ZETA" in basket_note
        and "ADJA" not in basket_note
        and "CTXA" not in basket_note
    )
    assert 'style="' not in card and "<script" not in card
