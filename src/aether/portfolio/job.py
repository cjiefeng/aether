"""Daily backtest + model-strategy job (spec §6.5, §9). No LLM; nothing leaves the machine.

1. Read closes and dividends (plain reads, no transaction held).
2. Hash the inputs (closes, dividends, config, `ALGO_VERSION`). If (as_of, hash) already has a
   run, stop: same inputs, same output.
3. Compute everything in memory: total-return series, every candidate's walk-forward backtest
   and metrics, the benchmarks, the per-profile selection and the current target weights.
4. One short `write_tx` stores the run and prunes old equity curves.

Stored JSON is canonical (sorted keys, floats rounded to `FLOAT_DP`), so the same input hash
gives byte-identical rows.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import Engine, delete, insert, select

from aether.config import PROFILES, StrategiesConfig
from aether.db.engine import write_tx
from aether.db.models import (
    dividends,
    prices_daily,
    strategy_curves,
    strategy_metrics,
    strategy_runs,
    strategy_weights,
    tickers,
)
from aether.db.types import utcnow_iso
from aether.portfolio.backtest import oos_start, run_backtest
from aether.portfolio.metrics import compute_metrics, equity
from aether.portfolio.select import Candidate, choose, qualify
from aether.portfolio.strategies import FAMILIES
from aether.portfolio.total_return import align, simple_returns, total_return_levels
from aether.runs import JobResult

# Bump when the computation changes, so identical prices still produce a fresh run.
ALGO_VERSION = "m4.1"
CORE = "QTUM"
BENCHMARKS = ("QQQ", "SOXX")
FLOAT_DP = 10
KEEP_CURVE_RUNS = 30
STORED_HISTORY_NOTE = (
    "Only about 2 years of prices are stored (the backfill limit), so every backtest is short."
)


@dataclass(frozen=True)
class Inputs:
    sleeve: tuple[str, ...]  # pure-plays, sorted
    closes: Mapping[str, list[tuple[str, float]]]
    dividends: Mapping[str, dict[str, Decimal]] = field(default_factory=dict)


def load_inputs(engine: Engine) -> Inputs:
    with engine.connect() as conn:
        sleeve = tuple(
            conn.execute(
                select(tickers.c.symbol)
                .where(tickers.c.type == "pure_play", tickers.c.active == 1)
                .order_by(tickers.c.symbol)
            ).scalars()
        )
        wanted = [CORE, *sleeve, *BENCHMARKS]
        closes: dict[str, list[tuple[str, float]]] = {s: [] for s in wanted}
        for sym, d, c in conn.execute(
            select(prices_daily.c.symbol, prices_daily.c.d, prices_daily.c.c)
            .where(prices_daily.c.symbol.in_(wanted))
            .order_by(prices_daily.c.symbol, prices_daily.c.d)
        ):
            closes[sym].append((d, c))
        divs: dict[str, dict[str, Decimal]] = {}
        for sym, ex, amt in conn.execute(
            select(dividends.c.symbol, dividends.c.ex_date, dividends.c.amount_micros)
            .where(dividends.c.symbol.in_(wanted))
            .order_by(dividends.c.symbol, dividends.c.ex_date)
        ):
            divs.setdefault(sym, {})[ex] = amt
    return Inputs(sleeve, closes, divs)


# --------------------------------------------------------------------------- canonical JSON


def _clean(obj: Any) -> Any:
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int | np.integer):
        return int(obj)
    if isinstance(obj, float | np.floating):
        f = float(obj)
        return round(f, FLOAT_DP) + 0.0 if math.isfinite(f) else None  # +0.0: no "-0.0"
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Mapping):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    raise TypeError(f"not serialisable: {type(obj).__name__}")


def canon(obj: Any) -> str:
    return json.dumps(_clean(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)


def input_hash(inputs: Inputs, config: StrategiesConfig) -> bytes:
    payload = {
        "algo": ALGO_VERSION,
        "config": config.model_dump(mode="json"),
        "sleeve": list(inputs.sleeve),
        "closes": {s: [[d, repr(c)] for d, c in rows] for s, rows in inputs.closes.items()},
        "dividends": inputs.dividends,
    }
    return hashlib.sha256(canon(payload).encode()).digest()


# --------------------------------------------------------------------------- compute


@dataclass(frozen=True)
class RunOutput:
    as_of: str
    summary: dict[str, Any]
    metrics: list[dict[str, Any]]  # strategy_metrics rows (JSON as Python objects)
    weights: dict[str, dict[str, float]]  # strategy_id -> symbol -> weight
    curves: dict[str, list[tuple[str, float]]]


def strategy_id(profile: str, family: str, q: float) -> str:
    return f"{profile}:{family}:q{round(q * 100):02d}"


def compute_run(inputs: Inputs, config: StrategiesConfig) -> RunOutput | None:
    """Everything the run stores, or None if QTUM's history is shorter than the warm-up."""
    params = config.backtest
    calendar = [d for d, _ in inputs.closes.get(CORE, [])]
    t0 = oos_start(params)
    if len(calendar) <= t0 + 1:
        return None
    cols = [CORE, *inputs.sleeve, *BENCHMARKS]
    levels = np.column_stack(
        [
            align(
                total_return_levels(
                    inputs.closes.get(s, []),
                    {ex: float(a) for ex, a in inputs.dividends.get(s, {}).items()},
                ),
                calendar,
            )
            for s in cols
        ]
    )
    R_all = simple_returns(levels)
    n_uni = 1 + len(inputs.sleeve)
    R = R_all[:, :n_uni]  # the investable universe; benchmarks are never held
    sleeve_idx = list(range(1, n_uni))
    oos_dates = calendar[t0:]
    bench_r = {s: np.nan_to_num(R_all[t0:, cols.index(s)], nan=0.0) for s in (CORE, *BENCHMARKS)}
    vs = {"QQQ": bench_r["QQQ"], CORE: bench_r[CORE]}
    ann = params.annualization

    metrics_rows: list[dict[str, Any]] = []
    bench_metrics: dict[str, Any] = {}
    for s in (CORE, *BENCHMARKS):
        m = compute_metrics(bench_r[s], oos_dates, vs, ann=ann)
        bench_metrics[s] = m
        metrics_rows.append(
            {
                "strategy_id": s,
                "kind": "benchmark",
                "profile": None,
                "family": None,
                "qtum_weight": None,
                "metrics": m,
                "qualifies": {},
            }
        )

    weights: dict[str, dict[str, float]] = {}
    returns_by_id: dict[str, Any] = {}
    profiles_out: dict[str, Any] = {}
    for profile in PROFILES:
        pp = config.profiles[profile]
        cands: list[Candidate] = []
        for family in FAMILIES:
            for q in pp.qtum_grid:
                sid = strategy_id(profile, family, q)
                res = run_backtest(
                    family,
                    R,
                    calendar,
                    core=0,
                    sleeve=sleeve_idx,
                    qtum_weight=q,
                    cap=pp.max_per_name,
                    params=params,
                )
                m = compute_metrics(res.returns, oos_dates, vs, ann=ann, turnovers=res.turnovers)
                qual = qualify(m, bench_metrics[CORE], pp)
                cands.append(Candidate(sid, m, qual))
                returns_by_id[sid] = res.returns
                weights[sid] = {
                    cols[j]: min(1.0, float(w)) for j, w in enumerate(res.current) if w > 1e-12
                }
                metrics_rows.append(
                    {
                        "strategy_id": sid,
                        "kind": "candidate",
                        "profile": profile,
                        "family": family,
                        "qtum_weight": q,
                        "metrics": m,
                        "qualifies": qual,
                    }
                )
        q_vol = bench_metrics[CORE]["volatility"]
        q_dd = bench_metrics[CORE]["max_drawdown"]
        profiles_out[profile] = {
            **choose(cands, pp),
            "rank_metric": pp.rank_metric,
            "limits": {
                "volatility": None if pp.vol_limit_x is None else pp.vol_limit_x * q_vol,
                "max_drawdown": None
                if pp.max_dd_limit_pp is None
                else q_dd + pp.max_dd_limit_pp / 100.0,
            },
            "min_qtum": pp.min_qtum,
            "max_per_name": pp.max_per_name,
        }

    base = calendar[t0 - 1]

    def curve(r: Any) -> list[tuple[str, float]]:
        e = equity(r) * 100.0
        return list(zip([base, *oos_dates], e.tolist(), strict=True))

    curves = {s: curve(bench_r[s]) for s in (CORE, *BENCHMARKS)}
    for p in profiles_out.values():
        if p["strategy_id"]:
            curves[p["strategy_id"]] = curve(returns_by_id[p["strategy_id"]])

    history = []
    caveats = [
        f"Out-of-sample period {oos_dates[0]} to {oos_dates[-1]} ({len(oos_dates)} sessions); "
        f"the first {params.estimation_window} returns are the estimation window.",
        STORED_HISTORY_NOTE,
    ]
    for s in (CORE, *inputs.sleeve):
        rows = inputs.closes.get(s, [])
        history.append({"symbol": s, "sessions": len(rows), "first": rows[0][0] if rows else None})
        if len(rows) < len(calendar):
            caveats.append(
                f"{s} has {len(rows)} sessions of history"
                + (f" (since {rows[0][0]})" if rows else "")
                + f"; it joins a strategy once it has {params.min_sessions} daily returns."
            )
    summary = {
        "as_of": calendar[-1],
        "oos_start": oos_dates[0],
        "oos_end": oos_dates[-1],
        "oos_sessions": len(oos_dates),
        "universe": [CORE, *inputs.sleeve],
        "benchmarks": list(BENCHMARKS),
        "profiles": profiles_out,
        "history": history,
        "caveats": caveats,
    }
    return RunOutput(calendar[-1], summary, metrics_rows, weights, curves)


# --------------------------------------------------------------------------- job


def run_exists(engine: Engine, as_of: str, digest: bytes) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(strategy_runs.c.id).where(
                    strategy_runs.c.as_of == as_of, strategy_runs.c.input_hash == digest
                )
            ).first()
            is not None
        )


def store_run(engine: Engine, out: RunOutput, digest: bytes, config: StrategiesConfig) -> int:
    with write_tx(engine) as conn:
        run_id: int = conn.execute(
            insert(strategy_runs)
            .values(
                as_of=out.as_of,
                input_hash=digest,
                config=canon(config.model_dump(mode="json")),
                summary=canon(out.summary),
                created_at=utcnow_iso(),
            )
            .returning(strategy_runs.c.id)
        ).scalar_one()
        conn.execute(
            insert(strategy_metrics),
            [
                {
                    **row,
                    "run_id": run_id,
                    "metrics": canon(row["metrics"]),
                    "qualifies": canon(row["qualifies"]),
                }
                for row in out.metrics
            ],
        )
        wrows = [
            {"run_id": run_id, "strategy_id": sid, "symbol": sym, "weight": _clean(w)}
            for sid, ws in out.weights.items()
            for sym, w in sorted(ws.items())
        ]
        if wrows:
            conn.execute(insert(strategy_weights), wrows)
        conn.execute(
            insert(strategy_curves),
            [
                {"run_id": run_id, "series_id": sid, "points": canon(pts)}
                for sid, pts in sorted(out.curves.items())
            ],
        )
        keep = (
            select(strategy_runs.c.id)
            .order_by(strategy_runs.c.id.desc())
            .limit(KEEP_CURVE_RUNS)
            .scalar_subquery()
        )
        conn.execute(delete(strategy_curves).where(strategy_curves.c.run_id.not_in(keep)))
    return run_id


def run_strategies(engine: Engine, config: StrategiesConfig) -> JobResult:
    inputs = load_inputs(engine)
    calendar = inputs.closes.get(CORE, [])
    if not calendar:
        return JobResult(warning="no QTUM prices yet; backtest skipped")
    digest = input_hash(inputs, config)
    if run_exists(engine, calendar[-1][0], digest):
        return JobResult()  # identical inputs: nothing new to store
    out = compute_run(inputs, config)
    if out is None:
        return JobResult(
            warning=f"QTUM has {len(calendar)} sessions; the backtest needs more than "
            f"{oos_start(config.backtest) + 1}"
        )
    store_run(engine, out, digest, config)
    return JobResult(rows_written=1 + len(out.metrics))
