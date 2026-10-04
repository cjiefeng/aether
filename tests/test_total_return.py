"""Total-return series: dividends raise total return by exactly the reinvested amount."""

from __future__ import annotations

import math

import numpy as np
import pytest

from aether.portfolio.total_return import align, simple_returns, total_return_levels

DAYS = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09"]


def test_flat_price_dividend_raises_total_return_by_yield() -> None:
    closes = [(d, 50.0) for d in DAYS]
    lv = total_return_levels(closes, {"2026-03-04": 1.0})
    assert lv[-1][1] == pytest.approx(1.02)  # $1 on $50 = +2%
    assert [v for _, v in lv[:2]] == [1.0, 1.0]
    assert total_return_levels(closes, {})[-1][1] == 1.0


def test_dividend_with_price_drop_on_ex_date() -> None:
    # The price falls by the dividend on the ex-date: total return is flat, price return isn't.
    closes = [(DAYS[0], 50.0), (DAYS[1], 49.0), (DAYS[2], 49.0)]
    lv = total_return_levels(closes, {DAYS[1]: 1.0})
    assert lv[1][1] == pytest.approx(1.0)
    assert lv[2][1] == pytest.approx(1.0)


def test_non_session_ex_date_applies_next_session() -> None:
    closes = [(d, 50.0) for d in DAYS]
    weekend = "2026-03-07"  # Saturday -> applies Monday 2026-03-09
    lv = dict(total_return_levels(closes, {weekend: 0.5}))
    assert lv["2026-03-06"] == 1.0
    assert lv["2026-03-09"] == pytest.approx(1.01)


def test_dividends_outside_the_series_are_ignored() -> None:
    closes = [(d, 50.0) for d in DAYS]
    lv = total_return_levels(closes, {"2026-03-02": 1.0, "2026-02-01": 1.0, "2026-04-01": 1.0})
    assert lv[-1][1] == 1.0  # on/before the first session, or after the last


def test_align_nan_before_listing_and_forward_fills_gaps() -> None:
    levels = [(DAYS[2], 1.0), (DAYS[4], 1.1)]  # listed on day 2; day 3 missing
    out = align(levels, DAYS)
    assert all(math.isnan(x) for x in out[:2])
    assert list(out[2:]) == [1.0, 1.0, 1.1, 1.1]
    r = simple_returns(out)
    assert math.isnan(r[2]) and r[3] == 0.0 and r[4] == pytest.approx(0.1)


def test_simple_returns_2d() -> None:
    lv = np.array([[1.0, np.nan], [1.1, 2.0], [1.21, 1.0]])
    r = simple_returns(lv)
    assert np.isnan(r[0]).all() and np.isnan(r[1, 1])
    assert r[2] == pytest.approx([0.1, -0.5])
