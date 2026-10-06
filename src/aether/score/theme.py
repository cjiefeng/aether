"""QTUM theme decomposition (spec §6.1): what drives QTUM. Deterministic; no LLM.

On QTUM's own sessions, daily total-return returns of QTUM are regressed (OLS with intercept) on
SOXX (semis), QQQ (broad tech) and an equal-weighted basket of the pure-plays, over the last
`WINDOW` sessions. A pure-play joins the basket on a day once it has `MIN_SESSIONS` returns before
that day.

Reported:
- betas with standard errors, R^2 and the basket's partial R^2
  = (SSE without the basket - SSE with it) / SSE without it;
- attribution over the last 30 and 90 sessions: beta_k x sum of factor k's daily returns for semis,
  tech and quantum, alpha x n, the residual, and QTUM's own sum of daily returns (sums of simple
  returns, so the parts add up exactly);
- the watchlist's combined weight in QTUM's latest holdings snapshot, as a cross-check.

The QTUM stance (M10) is labelled "quantum-sleeve view" and cites this.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import Engine, func, select

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import qtum_holdings, theme_decomposition, tickers
from aether.db.types import utcnow_iso
from aether.portfolio.total_return import align, simple_returns
from aether.runs import JobResult
from aether.score.prices import PriceSeries, load_series

WINDOW = 120
MIN_SESSIONS = 60
MIN_OBS = 60  # regression observations needed to report anything
ATTRIBUTION_WINDOWS = (30, 90)
FACTORS = ("semis", "tech", "quantum")
FACTOR_SYMBOLS = {"semis": "SOXX", "tech": "QQQ"}
DP = 10


def _r(x: float) -> float:
    return round(x, DP) + 0.0


@dataclass(frozen=True)
class Decomposition:
    as_of: str
    n: int
    betas: dict[str, Any]
    attribution: dict[str, Any]
    r2: float | None
    partial_r2: float | None
    members: list[str]


def basket_returns(
    member_returns: Mapping[str, NDArray[np.float64]], min_sessions: int
) -> tuple[NDArray[np.float64], list[list[str]]]:
    """Equal-weighted basket return per day over the members eligible that day."""
    syms = sorted(member_returns)
    t = len(next(iter(member_returns.values()))) if syms else 0
    out = np.full(t, np.nan)
    who: list[list[str]] = [[] for _ in range(t)]
    for i in range(t):
        vals = []
        for s in syms:
            r = member_returns[s]
            if math.isnan(r[i]):
                continue
            if int(np.sum(~np.isnan(r[:i]))) >= min_sessions:
                vals.append(r[i])
                who[i].append(s)
        if vals:
            out[i] = float(np.mean(vals))
    return out, who


def _ols(y: NDArray[np.float64], x: NDArray[np.float64]) -> tuple[NDArray[np.float64], float]:
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ coef
    return coef, float(resid @ resid)


def decompose(
    days: Sequence[str],
    qtum: NDArray[np.float64],
    semis: NDArray[np.float64],
    tech: NDArray[np.float64],
    members: Mapping[str, NDArray[np.float64]],
    *,
    window: int = WINDOW,
    min_sessions: int = MIN_SESSIONS,
) -> Decomposition | None:
    """All arrays are daily returns on `days` (NaN where missing)."""
    basket, who = basket_returns(members, min_sessions)
    idx = [
        i
        for i in range(max(0, len(days) - window), len(days))
        if not any(math.isnan(a[i]) for a in (qtum, semis, tech, basket))
    ]
    if len(idx) < MIN_OBS:
        return None
    y = qtum[idx]
    f = {"semis": semis[idx], "tech": tech[idx], "quantum": basket[idx]}
    x = np.column_stack([np.ones(len(idx)), f["semis"], f["tech"], f["quantum"]])
    coef, sse = _ols(y, x)
    _, sse_reduced = _ols(y, x[:, :3])
    sst = float(np.sum((y - y.mean()) ** 2))
    dof = len(idx) - x.shape[1]
    cov = (sse / dof) * np.linalg.pinv(x.T @ x)
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    names = ("alpha", *FACTORS)
    betas = {n: {"value": _r(float(coef[k])), "se": _r(float(se[k]))} for k, n in enumerate(names)}

    attribution: dict[str, Any] = {}
    for w in ATTRIBUTION_WINDOWS:
        sub = list(range(max(0, len(idx) - w), len(idx)))
        parts = {n: float(coef[k + 1] * np.sum(f[n][sub])) for k, n in enumerate(FACTORS)}
        alpha = float(coef[0] * len(sub))
        actual = float(np.sum(y[sub]))
        resid = actual - sum(parts.values()) - alpha
        explained = {**parts, "alpha": alpha, "residual": resid}
        attribution[str(w)] = {
            "sessions": len(sub),
            "start": days[idx[sub[0]]],
            "end": days[idx[sub[-1]]],
            "actual": _r(actual),
            **{k: _r(v) for k, v in explained.items()},
            "quantum_share": _r(parts["quantum"] / actual) if abs(actual) > 1e-9 else None,
        }
    return Decomposition(
        as_of=days[idx[-1]],
        n=len(idx),
        betas=betas,
        attribution=attribution,
        r2=_r(1.0 - sse / sst) if sst > 0 else None,
        partial_r2=_r((sse_reduced - sse) / sse_reduced) if sse_reduced > 0 else None,
        members=who[idx[-1]],
    )


def _returns_on(s: PriceSeries, cal: Sequence[str]) -> NDArray[np.float64]:
    return simple_returns(align(list(zip(s.days, s.levels, strict=True)), cal))


def run_theme(engine: Engine) -> JobResult:
    with engine.connect() as conn:
        pure = list(
            conn.execute(
                select(tickers.c.symbol)
                .where(tickers.c.type == "pure_play")
                .order_by(tickers.c.symbol)
            ).scalars()
        )
        series = load_series(conn, ["QTUM", "SOXX", "QQQ", *pure])
        snap = conn.execute(select(func.max(qtum_holdings.c.snapshot_date))).scalar()
        weight = None
        if snap is not None:
            w = conn.execute(
                select(func.sum(qtum_holdings.c.weight)).where(
                    qtum_holdings.c.snapshot_date == snap,
                    qtum_holdings.c.holding_symbol.in_(pure),
                )
            ).scalar()
            weight = None if w is None else _r(float(w) / 100.0)  # snapshot weights are percent
    cal = list(series["QTUM"].days)
    if not cal:
        return JobResult(warning="no QTUM prices yet")
    d = decompose(
        cal,
        _returns_on(series["QTUM"], cal),
        _returns_on(series["SOXX"], cal),
        _returns_on(series["QQQ"], cal),
        {s: _returns_on(series[s], cal) for s in pure},
    )
    if d is None:
        return JobResult(warning="not enough overlapping sessions for the theme regression")
    row = {
        "as_of": d.as_of,
        "n_sessions": d.n,
        "betas": json.dumps(d.betas, sort_keys=True),
        "attribution": json.dumps(d.attribution, sort_keys=True),
        "r2": d.r2,
        "quantum_partial_r2": d.partial_r2,
        "watchlist_weight_in_qtum": weight,
        "basket_members": json.dumps(d.members),
        "created_at": utcnow_iso(),
    }
    with write_tx(engine) as conn:
        upsert(conn, theme_decomposition, [row], key_cols=["as_of"])
    return JobResult(rows_written=1)
