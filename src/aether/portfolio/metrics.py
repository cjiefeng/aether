"""Risk/return metrics on daily simple returns (spec §6.5). Pure numpy; no LLM.

Conventions (shown on the Strategies page):
- `ann` sessions per year (252). Risk-free rate = 0 for Sharpe, Sortino and alpha.
- Volatility: sample std (ddof=1) x sqrt(ann). Downside deviation: RMS of min(r, 0) x sqrt(ann).
- CAGR: (1 + total return) ^ (ann / n) - 1.
- Max drawdown: the largest peak-to-trough fall of the equity curve, as a positive fraction.
  Its duration counts sessions from that peak until the curve regains it (or to the end, with
  `max_dd_recovered = False`).
- VaR95 / CVaR95: historical, as positive daily losses. VaR = -5th percentile (linear
  interpolation); CVaR = -mean of returns at or below that percentile.
- Sharpe = mean / std x sqrt(ann). Sortino = mean x ann / downside deviation. Calmar = CAGR /
  max drawdown.
- vs a benchmark b: beta = cov(r, b) / var(b); Jensen's alpha = (mean r - beta x mean b) x ann;
  tracking error = std(r - b) x sqrt(ann); information ratio = mean(r - b) x ann / TE.
- Up/down capture vs QQQ: mean(r) / mean(b) over the days b > 0 (up) or b < 0 (down).
- Months: calendar months, daily returns compounded (partial first/last months included).

Undefined values (division by zero, too few points) are None.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from itertools import groupby

import numpy as np
from numpy.typing import NDArray

Vec = NDArray[np.float64]
Metrics = dict[str, float | int | bool | None]


def _div(a: float, b: float) -> float | None:
    if b == 0 or not math.isfinite(a) or not math.isfinite(b):
        return None
    return a / b


def equity(r: Vec) -> Vec:
    """Equity curve starting at 1.0 before the first return (length n + 1)."""
    out: Vec = np.concatenate(([1.0], np.cumprod(1.0 + r)))
    return out


def drawdowns(r: Vec) -> Vec:
    e = equity(r)
    out: Vec = e / np.maximum.accumulate(e) - 1.0
    return out


def max_drawdown(r: Vec) -> tuple[float, int, bool]:
    """(max drawdown as a positive fraction, duration in sessions, recovered)."""
    dd = drawdowns(r)
    trough = int(np.argmin(dd))
    if dd[trough] >= 0:
        return 0.0, 0, True
    peak = int(np.flatnonzero(dd[: trough + 1] >= 0)[-1])
    after = np.flatnonzero(dd[trough:] >= 0)
    if after.size:
        return float(-dd[trough]), int(trough + after[0] - peak), True
    return float(-dd[trough]), int(len(dd) - 1 - peak), False


def monthly_returns(r: Vec, dates: Sequence[str]) -> list[float]:
    out: list[float] = []
    for _, grp in groupby(range(len(r)), key=lambda i: dates[i][:7]):
        idx = list(grp)
        out.append(float(np.prod(1.0 + r[idx]) - 1.0))
    return out


def relative(r: Vec, b: Vec, ann: int) -> Metrics:
    if len(r) < 2:
        return {"beta": None, "alpha": None, "tracking_error": None, "information_ratio": None}
    var_b = float(np.var(b, ddof=1))
    beta = _div(float(np.cov(r, b, ddof=1)[0, 1]), var_b)
    alpha = None if beta is None else (float(r.mean()) - beta * float(b.mean())) * ann
    active = r - b
    te = float(np.std(active, ddof=1)) * math.sqrt(ann)
    ir = _div(float(active.mean()) * ann, te)
    return {"beta": beta, "alpha": alpha, "tracking_error": te, "information_ratio": ir}


def capture(r: Vec, b: Vec) -> tuple[float | None, float | None]:
    up, down = b > 0, b < 0
    up_c = _div(float(r[up].mean()), float(b[up].mean())) if up.any() else None
    down_c = _div(float(r[down].mean()), float(b[down].mean())) if down.any() else None
    return up_c, down_c


def compute_metrics(
    r: Vec,
    dates: Sequence[str],
    benchmarks: Mapping[str, Vec],
    *,
    ann: int,
    capture_vs: str = "QQQ",
    turnovers: Sequence[float] | None = None,
) -> Metrics:
    """All §6.5 metrics for one return series. `benchmarks` maps a symbol to its returns over
    the same sessions; beta/alpha/TE/IR are reported per benchmark (`beta_QQQ`, ...)."""
    n = len(r)
    if n < 2:
        raise ValueError("need at least 2 returns")
    total = float(np.prod(1.0 + r) - 1.0)
    mean = float(r.mean())
    std = float(np.std(r, ddof=1))
    vol = std * math.sqrt(ann)
    dd_dev = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2))) * math.sqrt(ann)
    cagr = (1.0 + total) ** (ann / n) - 1.0 if total > -1.0 else -1.0
    mdd, mdd_len, recovered = max_drawdown(r)
    q05 = float(np.percentile(r, 5))
    months = monthly_returns(r, dates)
    m: Metrics = {
        "sessions": n,
        "total_return": total,
        "cagr": cagr,
        "volatility": vol,
        "downside_deviation": dd_dev,
        "max_drawdown": mdd,
        "max_dd_duration": mdd_len,
        "max_dd_recovered": recovered,
        "var95": -q05,
        "cvar95": -float(r[r <= q05].mean()),
        "sharpe": None,
        "sortino": _div(mean * ann, dd_dev),
        "calmar": _div(cagr, mdd),
        "worst_month": min(months),
        "pct_positive_months": sum(1 for x in months if x > 0) / len(months),
    }
    sharpe = _div(mean, std)
    m["sharpe"] = None if sharpe is None else sharpe * math.sqrt(ann)
    for sym, b in benchmarks.items():
        for k, v in relative(r, b, ann).items():
            m[f"{k}_{sym}"] = v
    if capture_vs in benchmarks:
        up, down = capture(r, benchmarks[capture_vs])
        m[f"up_capture_{capture_vs}"] = up
        m[f"down_capture_{capture_vs}"] = down
    if turnovers is not None:
        later = list(turnovers[1:])  # the initial allocation isn't a rebalance
        m["avg_turnover"] = sum(later) / len(later) if later else 0.0
        m["rebalances"] = len(later)
    return m
