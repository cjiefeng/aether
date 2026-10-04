"""M1 acceptance: idempotent re-runs; simulated yfinance failure -> fallback serves and the
provider is recorded per row and per job run. All prices are synthetic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest
from sqlalchemy import Engine, func, select

from aether.db.models import job_runs, prices_daily
from aether.ingest.prices import BACKFILL_DAYS, REVISION_OVERLAP_DAYS, ingest_prices, plan_ranges
from aether.providers.prices import Bar, FailoverPriceProvider, ProviderError
from aether.runs import run_job
from tests.conftest import seed_tickers

TODAY = date(2026, 10, 2)


@dataclass
class SeriesProvider:
    """Serves a deterministic synthetic series: close = base + day index."""

    name: str
    base: float = 10.0
    fail: bool = False
    calls: list[tuple[str, date, date]] = field(default_factory=list)

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
        self.calls.append((symbol, start, end))
        if self.fail:
            raise ProviderError(f"{self.name}: synthetic outage")
        out, d = [], start
        while d <= end:
            if d.weekday() < 5:
                c = self.base + (d - start).days * 0.01
                out.append(Bar(d, c, c + 0.5, c - 0.5, c, 1000, self.name))
            d += timedelta(days=1)
        return out


def _count(engine: Engine, **where: str) -> int:
    q = select(func.count()).select_from(prices_daily)
    for k, v in where.items():
        q = q.where(prices_daily.c[k] == v)
    with engine.connect() as conn:
        return int(conn.execute(q).scalar_one())


@pytest.fixture
def seeded(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, [("ACME", "pure_play"), ("EXMP", "pure_play")])
    return rw_engine


def test_plan_ranges_backfill_incremental_and_full_refresh() -> None:
    last = {"ACME": date(2026, 9, 30)}
    r = plan_ranges(["ACME", "EXMP"], last, TODAY, full_refresh=False)
    assert r["EXMP"] == (TODAY - timedelta(days=BACKFILL_DAYS), TODAY)
    assert r["ACME"] == (date(2026, 9, 30) - timedelta(days=REVISION_OVERLAP_DAYS), TODAY)
    full = plan_ranges(["ACME"], last, TODAY, full_refresh=True)
    assert full["ACME"][0] == TODAY - timedelta(days=BACKFILL_DAYS)


def test_backfill_then_rerun_is_idempotent(seeded: Engine) -> None:
    p = SeriesProvider("synthetic")
    first = ingest_prices(seeded, p, today=TODAY)
    n = _count(seeded)
    assert n == first.rows_written and n > 2 * 500  # ~2 years of weekdays x 2 symbols
    ingest_prices(seeded, p, today=TODAY)
    ingest_prices(seeded, p, today=TODAY, full_refresh=True)
    assert _count(seeded) == n
    # The second run only re-fetched the overlap window (TODAY is a Friday with a bar).
    assert p.calls[2][1] == TODAY - timedelta(days=REVISION_OVERLAP_DAYS)


def test_revised_bars_update_in_place(seeded: Engine) -> None:
    ingest_prices(seeded, SeriesProvider("synthetic", base=10.0), symbols=["ACME"], today=TODAY)
    n = _count(seeded)
    ingest_prices(seeded, SeriesProvider("synthetic", base=20.0), symbols=["ACME"], today=TODAY)
    assert _count(seeded) == n
    with seeded.connect() as conn:
        c = conn.execute(
            select(prices_daily.c.c).where(
                prices_daily.c.symbol == "ACME", prices_daily.c.d == TODAY.isoformat()
            )
        ).scalar_one()
    assert c >= 20.0


def test_simulated_yfinance_failure_fallback_serves_and_provider_recorded(seeded: Engine) -> None:
    yf = SeriesProvider("yfinance", fail=True)
    massive = SeriesProvider("massive", base=50.0)
    provider = FailoverPriceProvider(yf, massive)
    result = run_job(seeded, "prices", lambda: ingest_prices(seeded, provider, today=TODAY))
    assert result is not None and result.provider == "massive"
    total = _count(seeded)
    assert total > 0 and _count(seeded, provider="massive") == total
    with seeded.connect() as conn:
        run = conn.execute(select(job_runs).where(job_runs.c.job == "prices")).one()
    assert run.status == "ok" and run.provider == "massive"
    assert len(yf.calls) == 2  # tried for both symbols (threshold 3 not reached)


def test_partial_failure_keeps_other_symbols(seeded: Engine) -> None:
    @dataclass
    class OnlyAcme(SeriesProvider):
        def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
            if symbol == "EXMP":
                raise ProviderError("synthetic EXMP outage")
            return super().fetch_daily(symbol, start, end)

    result = ingest_prices(seeded, OnlyAcme("synthetic"), today=TODAY)
    assert result.rows_written == _count(seeded, symbol="ACME") > 0
    assert result.warning and "EXMP" in result.warning


def test_all_symbols_failing_fails_the_job(seeded: Engine) -> None:
    provider = SeriesProvider("synthetic", fail=True)
    assert run_job(seeded, "prices", lambda: ingest_prices(seeded, provider, today=TODAY)) is None
    with seeded.connect() as conn:
        assert conn.execute(select(job_runs.c.status)).scalar_one() == "failed"
    assert _count(seeded) == 0


def test_invalid_bars_are_dropped(seeded: Engine) -> None:
    @dataclass
    class Junk:
        name: str = "synthetic"

        def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
            return [
                Bar(TODAY, 10, 11, 9, 10, 100, "synthetic"),
                Bar(TODAY - timedelta(days=1), 10, 9, 11, 10, 100, "synthetic"),  # h < l
                Bar(TODAY - timedelta(days=2), 0, 1, 0, 0, 100, "synthetic"),  # non-positive
                Bar(TODAY + timedelta(days=1), 10, 11, 9, 10, 100, "synthetic"),  # future
            ]

    assert ingest_prices(seeded, Junk(), symbols=["ACME"], today=TODAY).rows_written == 1
