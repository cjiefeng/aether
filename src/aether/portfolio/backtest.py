"""Walk-forward backtest, no look-ahead (spec §6.5).

- Calendar: QTUM's sessions. `R[t]` is the return from session t-1 to t.
- In-sample warm-up: the first `estimation_window` returns. The first out-of-sample (OOS)
  session is `t0 = estimation_window + 1`; metrics use only OOS sessions.
- Rebalance on t0 (initial allocation, charged like any other rebalance) and on the first
  session of each calendar month. Weights for session t come from `R[:t]` only.
- Between rebalances the weights drift with returns.
- Cost: `cost_bps` per unit of turnover, where turnover = sum |target - drifted weights|,
  deducted from that session's return.
- A NaN return for a held name counts as 0 (it can't happen after listing, since levels are
  forward-filled).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from aether.config import BacktestParams
from aether.portfolio.strategies import Family, target_weights

Vec = NDArray[np.float64]


@dataclass(frozen=True)
class BacktestResult:
    returns: Vec  # OOS daily returns after costs, aligned with dates[t0:]
    turnovers: list[float]  # per rebalance, the initial allocation first
    rebalance_days: list[int]  # session indexes
    applied: dict[int, Vec]  # rebalance session -> target weights held from that session
    current: Vec  # weights for the session after the last one (the current target)


def oos_start(params: BacktestParams) -> int:
    return params.estimation_window + 1


def run_backtest(
    family: Family,
    R: NDArray[np.float64],
    dates: list[str],
    *,
    core: int,
    sleeve: list[int],
    qtum_weight: float,
    cap: float,
    params: BacktestParams,
    floor: float = 0.0,
) -> BacktestResult:
    T, N = R.shape
    t0 = oos_start(params)
    if t0 >= T:
        raise ValueError(f"need more than {t0} sessions, have {T}")

    def weights_for(t: int) -> Vec:
        return target_weights(
            family,
            R,
            t,
            core=core,
            sleeve=sleeve,
            qtum_weight=qtum_weight,
            cap=cap,
            params=params,
            floor=floor,
        )

    cost_rate = params.cost_bps / 10_000.0
    w = np.zeros(N)
    out = np.empty(T - t0)
    turnovers: list[float] = []
    rebalance_days: list[int] = []
    applied: dict[int, Vec] = {}
    for t in range(t0, T):
        cost = 0.0
        if t == t0 or dates[t][:7] != dates[t - 1][:7]:
            target = weights_for(t)
            turnover = float(np.abs(target - w).sum())
            cost = turnover * cost_rate
            turnovers.append(turnover)
            rebalance_days.append(t)
            applied[t] = target
            w = target
        r = np.nan_to_num(R[t], nan=0.0)
        gross = float(w @ r)
        out[t - t0] = gross - cost
        grown = w * (1.0 + r)
        total = float(grown.sum())
        w = grown / total if total > 0 else w
    current = weights_for(T)
    return BacktestResult(out, turnovers, rebalance_days, applied, current)
