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
