"""Price providers: yfinance frame parsing, Massive HTTP (respx, synthetic ACME), failover."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
import pytest
import respx

from aether.providers.prices import (
    MASSIVE_BASE,
    US_EASTERN,
    Bar,
    FailoverPriceProvider,
    MassiveProvider,
    ProviderError,
    YFinanceProvider,
    bars_from_frame,
)

D1, D2 = date(2026, 9, 1), date(2026, 9, 2)


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=US_EASTERN).timestamp() * 1000)


# --- yfinance ---------------------------------------------------------------------------------


def _frame(rows: list[tuple[date, float, float, float, float, float]]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]).tz_localize("America/New_York") for r in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Adj Close": [r[4] for r in rows],
            "Volume": [r[5] for r in rows],
        },
        index=idx,
    )


def test_yfinance_frame_to_bars_skips_nan() -> None:
    frame = _frame([(D1, 10, 11, 9, 10.5, 1000), (D2, math.nan, 1, 1, 1, 0)])
    assert bars_from_frame(frame) == [Bar(D1, 10, 11, 9, 10.5, 1000, "yfinance")]
    assert bars_from_frame(_frame([])) == []
    noisy = bars_from_frame(_frame([(D1, 45.15999984741211, 46, 44, 45.15999984741211, 1)]))
    assert noisy[0].o == 45.16 and noisy[0].c == 45.16


def test_yfinance_provider_wraps_any_exception() -> None:
    def boom(_s: str, _a: date, _b: date) -> object:
        raise RuntimeError("synthetic rate limit")

    with pytest.raises(ProviderError, match="synthetic rate limit"):
        YFinanceProvider(history_fn=boom).fetch_daily("ACME", D1, D2)


def test_yfinance_provider_passes_range() -> None:
    seen: list[tuple[str, date, date]] = []

    def fake(s: str, a: date, b: date) -> pd.DataFrame:
        seen.append((s, a, b))
        return _frame([(D1, 10, 11, 9, 10.5, 1000)])

    bars = YFinanceProvider(history_fn=fake).fetch_daily("ACME", D1, D2)
    assert seen == [("ACME", D1, D2)] and len(bars) == 1


# --- Massive ----------------------------------------------------------------------------------

SYNTHETIC_KEY = "synthetic-test-key"  # not a real key


def _bar(d: date, c: float) -> dict[str, float | int]:
    return {"o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1234.0, "t": _ms(d), "n": 1, "vw": c}


@dataclass
class FakeClock:
    t: float = 0.0
    slept: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def _massive(clock: FakeClock | None = None) -> MassiveProvider:
    clock = clock or FakeClock()
    return MassiveProvider(SYNTHETIC_KEY, client=httpx.Client(), clock=clock.now, sleep=clock.sleep)


@respx.mock
def test_massive_parses_paginates_and_uses_bearer_header() -> None:
    page2 = f"{MASSIVE_BASE}/v2/aggs/ticker/ACME/range/1/day/x/y?cursor=abc"
    first = respx.get(f"{MASSIVE_BASE}/v2/aggs/ticker/ACME/range/1/day/2026-09-01/2026-09-02").mock(
        return_value=httpx.Response(
            200, json={"status": "OK", "results": [_bar(D1, 10.0)], "next_url": page2}
        )
    )
    second = respx.get(page2).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": [_bar(D2, 11.0)]})
    )
    clock = FakeClock()
    bars = _massive(clock).fetch_daily("ACME", D1, D2)
    assert [(b.d, b.c, b.provider) for b in bars] == [(D1, 10.0, "massive"), (D2, 11.0, "massive")]
    for route in (first, second):
        req = route.calls.last.request
        assert req.headers["authorization"] == f"Bearer {SYNTHETIC_KEY}"
        assert SYNTHETIC_KEY not in str(req.url)
    assert first.calls.last.request.url.params["adjusted"] == "true"
    assert clock.slept == [12.5]  # 5 calls/minute


@respx.mock
def test_massive_no_results_is_empty_not_error() -> None:
    respx.get(url__startswith=MASSIVE_BASE).mock(
        return_value=httpx.Response(200, json={"status": "OK", "resultsCount": 0})
    )
    assert _massive().fetch_daily("ACME", D1, D2) == []


@pytest.mark.parametrize("status", [401, 429, 500])
@respx.mock
def test_massive_http_errors_raise(status: int) -> None:
    respx.get(url__startswith=MASSIVE_BASE).mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(ProviderError, match=f"HTTP {status}"):
        _massive().fetch_daily("ACME", D1, D2)


@respx.mock
def test_massive_never_follows_next_url_to_another_host() -> None:
    respx.get(url__startswith=MASSIVE_BASE).mock(
        return_value=httpx.Response(
            200,
            json={"status": "OK", "results": [], "next_url": "https://evil.example.test/steal"},
        )
    )
    evil = respx.get(url__startswith="https://evil.example.test").mock(
        return_value=httpx.Response(200, json={})
    )
    with pytest.raises(ProviderError, match="another host"):
        _massive().fetch_daily("ACME", D1, D2)
    assert not evil.called


@respx.mock
def test_massive_transport_error_raises() -> None:
    respx.get(url__startswith=MASSIVE_BASE).mock(side_effect=httpx.ConnectError("synthetic"))
    with pytest.raises(ProviderError, match="ConnectError"):
        _massive().fetch_daily("ACME", D1, D2)


# --- failover ---------------------------------------------------------------------------------


@dataclass
class FakeProvider:
    name: str
    fail: bool = False
    empty: bool = False
    calls: list[str] = field(default_factory=list)

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
        self.calls.append(symbol)
        if self.fail:
            raise ProviderError(f"{self.name} synthetic failure")
        if self.empty:
            return []
        return [Bar(start, 10, 11, 9, 10, 100, self.name)]


@dataclass
class Now:
    t: datetime = datetime(2026, 10, 4, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


def test_failover_serves_from_fallback_and_trips_after_threshold() -> None:
    yf, mv, now = FakeProvider("yfinance", fail=True), FakeProvider("massive"), Now()
    fo = FailoverPriceProvider(yf, mv, threshold=3, now=now)
    for sym in ("A", "B", "C", "D", "E"):
        assert fo.fetch_daily(sym, D1, D2)[0].provider == "massive"
    assert yf.calls == ["A", "B", "C"]  # tripped: D and E skip the primary
    assert mv.calls == ["A", "B", "C", "D", "E"]

    # After the probe interval the primary is tried again and, if healthy, takes over.
    yf.fail = False
    now.t += timedelta(hours=25)
    assert fo.fetch_daily("F", D1, D2)[0].provider == "yfinance"
    assert fo.fetch_daily("G", D1, D2)[0].provider == "yfinance"
    assert fo.consecutive_failures == 0 and fo.tripped_at is None


def test_failed_probe_restarts_timer() -> None:
    yf, mv, now = FakeProvider("yfinance", fail=True), FakeProvider("massive"), Now()
    fo = FailoverPriceProvider(yf, mv, threshold=1, now=now)
    fo.fetch_daily("A", D1, D2)
    now.t += timedelta(hours=25)
    fo.fetch_daily("B", D1, D2)  # probe fails
    now.t += timedelta(hours=1)
    fo.fetch_daily("C", D1, D2)
    assert yf.calls == ["A", "B"]


def test_empty_primary_tries_fallback_without_counting_failure() -> None:
    yf, mv = FakeProvider("yfinance", empty=True), FakeProvider("massive")
    fo = FailoverPriceProvider(yf, mv, threshold=1)
    assert fo.fetch_daily("A", D1, D2)[0].provider == "massive"
    assert fo.consecutive_failures == 0 and fo.tripped_at is None


def test_both_fail_raises_with_both_messages() -> None:
    fo = FailoverPriceProvider(
        FakeProvider("yfinance", fail=True), FakeProvider("massive", fail=True)
    )
    with pytest.raises(
        ProviderError, match="yfinance synthetic failure; massive synthetic failure"
    ):
        fo.fetch_daily("A", D1, D2)


def test_no_fallback_never_trips() -> None:
    yf = FakeProvider("yfinance", fail=True)
    fo = FailoverPriceProvider(yf, None, threshold=1)
    for _ in range(3):
        with pytest.raises(ProviderError):
            fo.fetch_daily("A", D1, D2)
    assert len(yf.calls) == 3
