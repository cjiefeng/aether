"""Strategy families: a QTUM core weight plus a pure-play sleeve (spec §6.5).

`target_weights(R, t, ...)` returns the weights held during session t. It reads only `R[:t]`
(returns up to and including session t-1), so nothing at or after t can leak in.

Per-name caps are applied by water-filling. Sleeve weight the caps can't place goes to QTUM:
there is no cash sleeve, and a safer profile means more QTUM.

Minimum weight per name (M14, spec §6.5): every eligible name first gets the floor
`f = min(min_per_name, sleeve / n)`; the family then allocates only the rest of the sleeve, with
each name's total kept within the cap (so the family sees a cap of `cap - f`). Momentum's names
outside the top N therefore sit at the floor. The research overlay runs later and can still cut a
name to 0.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from numpy.typing import NDArray

from aether.config import BacktestParams

Family = Literal["core_equal", "core_inv_vol", "core_min_var", "core_momentum"]
FAMILIES: tuple[Family, ...] = ("core_equal", "core_inv_vol", "core_min_var", "core_momentum")

Vec = NDArray[np.float64]
VOL_FLOOR = 1e-12
MIN_VAR_TOL = 1e-12


def cap_weights(raw: Vec, total: float, cap: float) -> Vec:
    """Scale non-negative `raw` to sum to `total`, with no entry above `cap`. Capped names are
    fixed at `cap` and their excess is redistributed pro rata over the rest. If every name hits
    the cap the result sums to less than `total`."""
    w = np.zeros_like(raw, dtype=float)
    active = raw > 0
    remaining = total
    while active.any() and remaining > 0:
        share = remaining * raw / raw[active].sum()
        over = active & (share > cap)
        if not over.any():
            w[active] = share[active]
            break
        w[over] = cap
        remaining -= cap * int(over.sum())
        active &= ~over
    return w


def project_capped_simplex(v: Vec, total: float, cap: float) -> Vec:
    """Exact Euclidean projection of v onto {0 <= w <= cap, sum(w) = total}.

    w = clip(v - tau, 0, cap), where f(tau) = sum(clip(v - tau, 0, cap)) = total. f is piecewise
    linear and non-increasing with breakpoints at v_i and v_i - cap, so tau is found by
    evaluating f at the sorted breakpoints and interpolating. If total >= n * cap the set is the
    single point w = cap."""
    n = len(v)
    if total >= n * cap:
        return np.full(n, cap)
    bps = np.unique(np.concatenate((v - cap, v)))  # ascending
    f = np.clip(v[None, :] - bps[:, None], 0.0, cap).sum(axis=1)  # non-increasing
    k = int(np.searchsorted(-f, -total, side="right")) - 1  # last breakpoint with f >= total
    if k >= len(bps) - 1:
        tau = float(bps[-1])
    else:
        f0, f1 = float(f[k]), float(f[k + 1])
        tau = (
            float(bps[k])
            if f0 == f1
            else float(bps[k]) + (f0 - total) * float(bps[k + 1] - bps[k]) / (f0 - f1)
        )
    out: Vec = np.clip(v - tau, 0.0, cap)
    return out


def min_variance(cov: NDArray[np.float64], total: float, cap: float, iterations: int) -> Vec:
    """Long-only minimum variance w'Σw on the capped simplex, by accelerated projected gradient
    (Nesterov/FISTA) with step 1/L, L = 2·λmax(Σ). Deterministic: fixed start (equal weight),
    fixed step, at most `iterations` steps, stopping early once a step moves no weight by more
    than `MIN_VAR_TOL`."""
    n = cov.shape[0]
    w = project_capped_simplex(np.full(n, total / n), total, cap)
    lam = float(np.linalg.eigvalsh(cov)[-1])
    if not np.isfinite(lam) or lam <= 0:
        return w
    step = 1.0 / (2.0 * lam)
    y, t = w, 1.0
    for _ in range(iterations):
        w_next = project_capped_simplex(y - step * 2.0 * (cov @ y), total, cap)
        if float(np.abs(w_next - w).max()) < MIN_VAR_TOL:
            return w_next
        t_next = (1.0 + (1.0 + 4.0 * t * t) ** 0.5) / 2.0
        y = w_next + ((t - 1.0) / t_next) * (w_next - w)
        w, t = w_next, t_next
    return w


def eligible(R: NDArray[np.float64], t: int, sleeve: list[int], min_sessions: int) -> list[int]:
    """Sleeve columns with >= min_sessions returns before session t."""
    past = R[:t]
    return [j for j in sleeve if np.count_nonzero(~np.isnan(past[:, j])) >= min_sessions]


def _window(R: NDArray[np.float64], t: int, j: int, n: int) -> Vec:
    col = R[max(0, t - n) : t, j]
    out: Vec = col[~np.isnan(col)]
    return out


def effective_floor(n: int, total: float, floor: float) -> float:
    """The per-name floor actually applied: shrunk to `total / n` when n floors don't fit."""
    if n == 0 or floor <= 0 or total <= 0:
        return 0.0
    return min(floor, total / n)


def sleeve_weights(
    family: Family,
    R: NDArray[np.float64],
    t: int,
    names: list[int],
    total: float,
    cap: float,
    params: BacktestParams,
    floor: float = 0.0,
) -> Vec:
    """Weights (summing to <= total) for the eligible `names`, in that order. Each name gets at
    least the (effective) floor and at most `cap`."""
    n = len(names)
    if n == 0 or total <= 0:
        return np.zeros(n)
    f = min(effective_floor(n, total, floor), cap)
    if f <= 0:
        return _method_weights(family, R, t, names, total, cap, params)
    rest = total - n * f
    out = np.full(n, f)
    if rest > 1e-15 and cap - f > 1e-15:
        out = out + _method_weights(family, R, t, names, rest, cap - f, params)
    return out


def _method_weights(
    family: Family,
    R: NDArray[np.float64],
    t: int,
    names: list[int],
    total: float,
    cap: float,
    params: BacktestParams,
) -> Vec:
    n = len(names)
    window = params.estimation_window
    if family == "core_equal":
        return cap_weights(np.ones(n), total, cap)
    if family == "core_inv_vol":
        vols = np.array([np.std(_window(R, t, j, window), ddof=1) for j in names])
        return cap_weights(1.0 / np.maximum(vols, VOL_FLOOR), total, cap)
    if family == "core_min_var":
        # Common window: the most recent rows where every eligible name has a return.
        common = min(window, *(len(_window(R, t, j, window)) for j in names))
        block = R[t - common : t][:, names]
        cov = np.atleast_2d(np.cov(block, rowvar=False, ddof=1))
        return min_variance(cov, total, cap, params.min_var_iterations)
    if family == "core_momentum":
        mom = np.array(
            [np.prod(1.0 + _window(R, t, j, params.momentum_lookback)) - 1.0 for j in names]
        )
        # Highest trailing return first; ties keep column order (stable sort).
        order = np.argsort(-mom, kind="stable")[: params.momentum_top_n]
        raw = np.zeros(n)
        raw[order] = 1.0
        return cap_weights(raw, total, cap)
    raise ValueError(f"unknown family {family!r}")


def target_weights(
    family: Family,
    R: NDArray[np.float64],
    t: int,
    *,
    core: int,
    sleeve: list[int],
    qtum_weight: float,
    cap: float,
    params: BacktestParams,
    floor: float = 0.0,
) -> Vec:
    """Full weight vector (columns of R) held during session t, from `R[:t]` only."""
    w = np.zeros(R.shape[1])
    names = eligible(R, t, sleeve, params.min_sessions)
    sw = sleeve_weights(family, R, t, names, 1.0 - qtum_weight, cap, params, floor)
    w[names] = sw
    w[core] = 1.0 - float(sw.sum())  # the core absorbs whatever the caps couldn't place
    return w
