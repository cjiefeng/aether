"""§6.5 metrics vs values computed by hand (plain Python arithmetic, not the module's numpy)."""

from __future__ import annotations

import math
import statistics

import numpy as np
import pytest

from aether.portfolio.metrics import compute_metrics, max_drawdown, monthly_returns

R = [0.01, -0.02, 0.03, -0.01, 0.02]
DATES = ["2026-01-29", "2026-01-30", "2026-02-02", "2026-02-03", "2026-02-04"]
ANN = 252


def _m(r: list[float] = R, bench: dict[str, list[float]] | None = None, **kw: object):
    b = {k: np.array(v) for k, v in (bench or {}).items()}
    return compute_metrics(np.array(r), DATES, b, ann=ANN, **kw)  # type: ignore[arg-type]


def test_return_and_risk_basics() -> None:
    m = _m()
    total = 1.01 * 0.98 * 1.03 * 0.99 * 1.02 - 1
    assert m["total_return"] == pytest.approx(total, abs=1e-12)
    assert m["cagr"] == pytest.approx((1 + total) ** (ANN / 5) - 1, rel=1e-12)
    sd = statistics.stdev(R)
    mean = statistics.fmean(R)
    assert m["volatility"] == pytest.approx(sd * math.sqrt(ANN), rel=1e-12)
    assert m["sharpe"] == pytest.approx(mean / sd * math.sqrt(ANN), rel=1e-12)
    dd_dev = math.sqrt((0.02**2 + 0.01**2) / 5) * math.sqrt(ANN)
    assert m["downside_deviation"] == pytest.approx(dd_dev, rel=1e-12)
    assert m["sortino"] == pytest.approx(mean * ANN / dd_dev, rel=1e-12)


def test_max_drawdown_and_duration() -> None:
    # Equity 1, 1.01, 0.9898, 1.0195, 1.0093, 1.0295: worst fall is 1.01 -> 0.9898 (-2%),
    # peak at index 1, regained at index 3 -> duration 2 sessions.
    m = _m()
    assert m["max_drawdown"] == pytest.approx(0.02, abs=1e-12)
    assert m["max_dd_duration"] == 2
    assert m["max_dd_recovered"] is True
    assert m["calmar"] == pytest.approx(m["cagr"] / 0.02, rel=1e-12)  # type: ignore[operator]


def test_unrecovered_drawdown_runs_to_end() -> None:
    dd, length, recovered = max_drawdown(np.array([0.10, -0.20, 0.05, 0.01]))
    assert dd == pytest.approx(0.20)
    assert length == 3  # peak at index 1 (after +10%), never regained by index 4
    assert recovered is False
    assert max_drawdown(np.array([0.01, 0.02])) == (0.0, 0, True)


def test_historical_var_and_cvar() -> None:
    # Sorted: -0.02, -0.01, 0.01, 0.02, 0.03. 5th pct (linear): -0.02 + 0.2 * 0.01 = -0.018.
    m = _m()
    assert m["var95"] == pytest.approx(0.018, abs=1e-12)
    assert m["cvar95"] == pytest.approx(0.02, abs=1e-12)  # mean of returns <= -0.018


def test_beta_alpha_tracking_error_capture() -> None:
    b = [0.004, -0.01, 0.012, -0.006, 0.009]
    r = [2 * x + 0.001 for x in b]
    m = _m(r, {"QQQ": b, "QTUM": b})
    assert m["beta_QQQ"] == pytest.approx(2.0, rel=1e-9)
    assert m["alpha_QQQ"] == pytest.approx(0.001 * ANN, rel=1e-9)
    active = [x - y for x, y in zip(r, b, strict=True)]
    te = statistics.stdev(active) * math.sqrt(ANN)
    assert m["tracking_error_QTUM"] == pytest.approx(te, rel=1e-9)
    assert m["information_ratio_QTUM"] == pytest.approx(
        statistics.fmean(active) * ANN / te, rel=1e-9
    )
    up_b = [x for x in b if x > 0]
    up_r = [2 * x + 0.001 for x in up_b]
    assert m["up_capture_QQQ"] == pytest.approx(statistics.fmean(up_r) / statistics.fmean(up_b))
    dn_b = [x for x in b if x < 0]
    dn_r = [2 * x + 0.001 for x in dn_b]
    assert m["down_capture_QQQ"] == pytest.approx(statistics.fmean(dn_r) / statistics.fmean(dn_b))


def test_months_and_turnover() -> None:
    months = monthly_returns(np.array(R), DATES)
    assert months == pytest.approx([1.01 * 0.98 - 1, 1.03 * 0.99 * 1.02 - 1])
    m = _m(turnovers=[1.0, 0.2, 0.4])
    assert m["worst_month"] == pytest.approx(1.01 * 0.98 - 1)
    assert m["pct_positive_months"] == 0.5
    assert m["avg_turnover"] == pytest.approx(0.3)  # the initial allocation is excluded
    assert m["rebalances"] == 2


def test_zero_volatility_is_none_not_error() -> None:
    m = _m([0.0] * 5, {"QQQ": [0.0] * 5})
    assert m["sharpe"] is None and m["sortino"] is None and m["beta_QQQ"] is None
    assert m["up_capture_QQQ"] is None
