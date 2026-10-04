"""Read-side computations on synthetic series."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from aether import market

TODAY = date(2026, 10, 2)


def test_rebase_and_window() -> None:
    s = [("2026-09-01", 50.0), ("2026-09-02", 55.0), ("2026-10-01", 60.0)]
    assert market.rebase(s) == [("2026-09-01", 100.0), ("2026-09-02", 110.0), ("2026-10-01", 120.0)]
    assert market.window(s, 30, TODAY) == [("2026-09-02", 55.0), ("2026-10-01", 60.0)]
    assert market.rebase([]) == []


def test_basket_equal_weight_with_late_joiner() -> None:
    acme = [("d1", 10.0), ("d2", 11.0), ("d3", 11.0)]  # +10%, 0%
    exmp = [("d2", 50.0), ("d3", 60.0)]  # joins d2; +20% on d3
    b = dict(market.basket_series({"ACME": acme, "EXMP": exmp}))
    assert b["d1"] == 100.0
    assert b["d2"] == pytest.approx(110.0)  # only ACME has a return on d2
    assert b["d3"] == pytest.approx(110.0 * 1.10)  # mean(0%, +20%) = +10%


def test_summarize() -> None:
    start = date(2025, 10, 1)
    series = [((start + timedelta(days=i)).isoformat(), 100.0) for i in range(366)]
    series.append(((start + timedelta(days=366)).isoformat(), 400.0))  # new high
    series.append((TODAY.isoformat(), 200.0))
    s = market.summarize("ACME", "pure_play", series, "synthetic", TODAY)
    assert s.last_c == 200.0 and s.chg_1d == pytest.approx(200 / 400 - 1)
    assert s.drawdown_52w == pytest.approx(-0.5)
    assert s.chg_30d is not None and not s.stale
    assert market.summarize("ACME", "pure_play", [], None, TODAY).stale


def test_staleness_thresholds() -> None:
    assert not market.symbol_stale("2026-09-28", TODAY)  # Mon -> Fri: 4 days
    assert market.symbol_stale("2026-09-27", TODAY)
    assert market.symbol_stale(None, TODAY)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    assert market.job_stale(None, now, market.PRICES_JOB_MAX_AGE)
    assert not market.job_stale("2026-10-04T00:00:00Z", now, market.PRICES_JOB_MAX_AGE)
    assert market.job_stale("2026-10-03T00:00:00Z", now, market.PRICES_JOB_MAX_AGE)
