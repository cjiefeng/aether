"""Dividend providers (yfinance frame, Massive HTTP via respx; synthetic values) and ingest."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pandas as pd
import pytest
import respx
from sqlalchemy import Engine, select

from aether.db.models import dividends
from aether.ingest.dividends import ingest_dividends
from aether.providers.dividends import (
    Dividend,
    FallbackDividends,
    MassiveDividends,
    YFinanceDividends,
)
from aether.providers.prices import MASSIVE_BASE, ProviderError
from tests.conftest import seed_tickers

SYNTHETIC_KEY = "synthetic-test-key"  # not a real key
TODAY = date(2026, 10, 2)
URL = f"{MASSIVE_BASE}/stocks/v1/dividends"


def _series(rows: list[tuple[str, float]]) -> pd.Series:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") for d, _ in rows])
    return pd.Series([v for _, v in rows], index=idx, dtype="float64")


def _massive() -> MassiveDividends:
    return MassiveDividends(SYNTHETIC_KEY, client=httpx.Client(), min_interval_s=0)


def test_yfinance_dividends_filtered_and_rounded() -> None:
    yf = YFinanceDividends(
        lambda _s: _series([("2024-01-05", 0.1), ("2026-03-20", 0.123456789), ("2026-06-20", 0)])
    )
    out = yf.fetch_dividends("QTUM", date(2025, 1, 1), TODAY)
    assert out == [Dividend(date(2026, 3, 20), Decimal("0.123457"), "yfinance")]


def test_yfinance_error_is_provider_error() -> None:
    def boom(_s: str) -> pd.Series:
        raise RuntimeError("synthetic")

    with pytest.raises(ProviderError, match="RuntimeError"):
        YFinanceDividends(boom).fetch_dividends("QTUM", date(2025, 1, 1), TODAY)


@respx.mock
def test_massive_parses_paginates_and_prefers_split_adjusted() -> None:
    page2 = f"{URL}?cursor=abc"
    first = respx.get(URL, params={"ticker": "QTUM"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "ticker": "QTUM",
                        "ex_dividend_date": "2026-03-20",
                        "cash_amount": 0.4,
                        "split_adjusted_cash_amount": 0.2,
                        "currency": "USD",
                    },
                    {
                        "ticker": "QTUM",
                        "ex_dividend_date": "2026-04-20",
                        "cash_amount": 1.0,
                        "currency": "CAD",
                    },
                ],
                "next_url": page2,
            },
        )
    )
    respx.get(URL, params={"cursor": "abc"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "ticker": "QTUM",
                        "ex_dividend_date": "2026-06-20",
                        "cash_amount": 0.3,
                        "currency": "USD",
                    }
                ],
            },
        )
    )
    out = _massive().fetch_dividends("QTUM", date(2025, 1, 1), TODAY)
    assert out == [
        Dividend(date(2026, 3, 20), Decimal("0.2"), "massive"),
        Dividend(date(2026, 6, 20), Decimal("0.3"), "massive"),
    ]
    req = first.calls.last.request
    assert req.headers["authorization"] == f"Bearer {SYNTHETIC_KEY}"
    assert SYNTHETIC_KEY not in str(req.url)
    assert req.url.params["ticker"] == "QTUM"
    assert req.url.params["ex_dividend_date.gte"] == "2025-01-01"


@respx.mock
def test_massive_page_cap_stops_a_cursor_loop() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": [], "next_url": URL})
    )
    with pytest.raises(ProviderError, match="pages"):
        _massive().fetch_dividends("QTUM", date(2025, 1, 1), TODAY)


@respx.mock
def test_massive_never_follows_next_url_to_another_host() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, json={"status": "OK", "results": [], "next_url": "https://evil.example.test/x"}
        )
    )
    with pytest.raises(ProviderError, match="another host"):
        _massive().fetch_dividends("QTUM", date(2025, 1, 1), TODAY)


class _Stub:
    def __init__(self, name: str, result: list[Dividend] | Exception) -> None:
        self.name, self.result, self.calls = name, result, 0

    def fetch_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_fallback_only_on_error_not_on_empty() -> None:
    empty, backup = _Stub("yfinance", []), _Stub("massive", [])
    assert FallbackDividends(empty, backup).fetch_dividends("ACME", TODAY, TODAY) == []
    assert backup.calls == 0  # most pure-plays pay nothing: empty is not a failure
    failing = _Stub("yfinance", ProviderError("synthetic"))
    good = [Dividend(date(2026, 3, 20), Decimal("0.2"), "massive")]
    assert (
        FallbackDividends(failing, _Stub("massive", good)).fetch_dividends("QTUM", TODAY, TODAY)
        == good
    )
    with pytest.raises(ProviderError):
        FallbackDividends(failing, None).fetch_dividends("QTUM", TODAY, TODAY)


def test_ingest_is_idempotent_and_records_provider(rw_engine: Engine) -> None:
    seed_tickers(rw_engine, [("QTUM", "etf"), ("ACME", "pure_play"), ("IBM", "context")])
    provider = _Stub("yfinance", [Dividend(date(2026, 3, 20), Decimal("0.2"), "yfinance")])
    r1 = ingest_dividends(rw_engine, provider, today=TODAY)
    r2 = ingest_dividends(rw_engine, provider, today=TODAY)
    assert r1.rows_written == r2.rows_written == 2  # QTUM + ACME (context tickers skipped)
    assert provider.calls == 4
    with rw_engine.connect() as conn:
        rows = conn.execute(
            select(dividends.c.symbol, dividends.c.amount_micros, dividends.c.provider)
        ).all()
    assert sorted(rows) == [
        ("ACME", Decimal("0.2"), "yfinance"),
        ("QTUM", Decimal("0.2"), "yfinance"),
    ]
    assert r1.provider == "yfinance"


def test_ingest_partial_and_total_failure(rw_engine: Engine) -> None:
    seed_tickers(rw_engine, [("QTUM", "etf")])
    with pytest.raises(ProviderError, match="all symbols failed"):
        ingest_dividends(rw_engine, _Stub("x", ProviderError("synthetic")), today=TODAY)
