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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import Connection, Engine, delete, func, insert, select

from aether.config import PROFILES, BacktestParams, ProfileParams, StrategiesConfig
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
from aether.portfolio.strategies import FAMILIES, Buckets, effective_floor, eligible
from aether.portfolio.total_return import align, simple_returns, total_return_levels
from aether.runs import JobResult

# Bump when the computation changes, so identical prices still produce a fresh run.
ALGO_VERSION = "m16"  # m5: fixed QTUM weight; m14: floor + adjacent; m16: buckets, families
CORE = "QTUM"
BENCHMARKS = ("QQQ", "SOXX")
FLOAT_DP = 10
KEEP_CURVE_RUNS = 30
STORED_HISTORY_NOTE = (
    "Only about 2 years of prices are stored (the backfill limit), so every backtest is short."
)


@dataclass(frozen=True)
class Inputs:
    sleeve: tuple[str, ...]  # active names of the configured sleeve types, sorted
    closes: Mapping[str, list[tuple[str, float]]]
    dividends: Mapping[str, dict[str, Decimal]] = field(default_factory=dict)
    # M16: symbol -> tickers.type for the sleeve; a symbol missing here counts as a pure-play.
    types: Mapping[str, str] = field(default_factory=dict)


def load_inputs(engine: Engine, sleeve_types: Sequence[str] = ("pure_play", "adjacent")) -> Inputs:
    with engine.connect() as conn:
        types = {
            r.symbol: r.type
            for r in conn.execute(
                select(tickers.c.symbol, tickers.c.type)
                .where(tickers.c.type.in_(list(sleeve_types)), tickers.c.active == 1)
                .order_by(tickers.c.symbol)
            )
        }
        sleeve = tuple(types)
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
    return Inputs(sleeve, closes, divs, types)


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


# Only what the backtest reads, so M5's rebalance/publish/overlay settings don't force a new run.
BACKTEST_CONFIG_KEYS = ("backtest", "profiles", "sleeve_types")


def config_digest(config_json: Mapping[str, Any]) -> bytes:
    """Digest of the backtest part of a strategies config: the current YAML
    (`config.model_dump(mode="json")`) and a stored run's `config` column compare directly."""
    part = {k: config_json.get(k) for k in BACKTEST_CONFIG_KEYS}
    return hashlib.sha256(canon(part).encode()).digest()


def run_config_digest(conn: Connection, run_id: int) -> bytes | None:
    raw = conn.execute(select(strategy_runs.c.config).where(strategy_runs.c.id == run_id)).scalar()
    return None if raw is None else config_digest(json.loads(raw))


def config_changed(engine: Engine, config: StrategiesConfig) -> bool:
    """True when the latest run was computed under a different backtest config than `config`
    (issue #13: a deploy that edits `strategies.yaml` must not keep showing the old run).
    False with no run yet; the usual catch-up handles that."""
    with engine.connect() as conn:
        run_id = conn.execute(select(func.max(strategy_runs.c.id))).scalar()
        stored = None if run_id is None else run_config_digest(conn, run_id)
    return stored is not None and stored != config_digest(config.model_dump(mode="json"))


def input_hash(inputs: Inputs, config: StrategiesConfig) -> bytes:
    payload = {
        "algo": ALGO_VERSION,
        "config": config.model_dump(mode="json", include=set(BACKTEST_CONFIG_KEYS)),
        "sleeve": list(inputs.sleeve),
        "types": [inputs.types.get(s, "pure_play") for s in inputs.sleeve],
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


def floor_note(
    R: Any, t: int, sleeve_idx: list[int], pp: ProfileParams, params: BacktestParams
) -> dict[str, Any]:
    """The current floor (spec §6.5): configured vs applied, and why it shrank, if it did."""
    n = len(eligible(R, t, sleeve_idx, params.min_sessions))
    sleeve = 1.0 - pp.qtum_weight
    applied = effective_floor(n, sleeve, pp.min_per_name)
    shrunk = n > 0 and applied < pp.min_per_name
    return {
        "configured": pp.min_per_name,
        "applied": applied,
        "eligible": n,
        "note": (
            f"The floor shrank from {pp.min_per_name:.1%} to {applied:.2%} per name: "
            f"{n} eligible names x {pp.min_per_name:.1%} is more than the "
            f"{sleeve:.0%} sleeve."
        )
        if shrunk
        else None,
    }


def bucket_floor_note(
    R: Any,
    t: int,
    buckets: Buckets,
    pp: ProfileParams,
    params: BacktestParams,
) -> dict[str, Any]:
    """The floor per bucket (M16): configured vs applied in each, and why it shrank, if it did."""
    sleeve = 1.0 - pp.qtum_weight
    n_adj = len(eligible(R, t, buckets.adjacent, params.min_sessions))
    n_pure = len(eligible(R, t, buckets.pure, params.min_sessions))
    adj_budget = min(buckets.adj_total, sleeve)
    adj = min(effective_floor(n_adj, adj_budget, pp.min_per_name), buckets.adj_cap)
    pure_budget = sleeve - min(adj_budget, n_adj * buckets.adj_cap)
    pure = min(effective_floor(n_pure, pure_budget, pp.min_per_name), pp.max_per_name)
    notes = [
        f"The {label} floor shrank from {pp.min_per_name:.1%} to {applied:.2%} per name: "
        f"{n} eligible names x {pp.min_per_name:.1%} is more than the {budget:.0%} bucket."
        for label, n, applied, budget in (
            ("adjacent", n_adj, adj, adj_budget),
            ("pure-play", n_pure, pure, pure_budget),
        )
        if n > 0 and applied < pp.min_per_name
    ]
    return {
        "configured": pp.min_per_name,
        "applied": min(adj, pure) if n_adj and n_pure else (adj if n_adj else pure),
        "applied_adjacent": adj,
        "applied_pure_play": pure,
        "eligible": n_adj + n_pure,
        "note": " ".join(notes) or None,
    }


def split(weights: Mapping[str, float], types: Mapping[str, str]) -> dict[str, float]:
    """QTUM / pure-play / adjacent shares of a target (M16), from `tickers.type`."""
    out = {"qtum": 0.0, "pure_play": 0.0, "adjacent": 0.0}
    for s, w in weights.items():
        key = "qtum" if s == CORE else ("adjacent" if types.get(s) == "adjacent" else "pure_play")
        out[key] += float(w)
    return out


def bucket_summary(
    weights: Mapping[str, float],
    types: Mapping[str, str],
    pp: ProfileParams,
    n_pure: int,
    n_adj: int,
) -> dict[str, Any]:
    """The base split, and for a bucketed profile the configured buckets, the note when sleeve
    weight had to go to QTUM, and the pure-play-share check on the base target."""
    sp = split(weights, types)
    out: dict[str, Any] = {
        "split": sp,
        "buckets": None,
        "bucket_note": None,
        "pure_play_flag": None,
    }
    if pp.pure_play_min_total is not None:
        out["pure_play_flag"] = sp["pure_play"] < pp.pure_play_min_total - 1e-9
    if not pp.buckets:
        return out
    assert pp.adjacent_max_total is not None and pp.adjacent_max_per_name is not None
    out["buckets"] = {
        "adjacent_max_total": pp.adjacent_max_total,
        "adjacent_max_per_name": pp.adjacent_max_per_name,
        "pure_play_max_per_name": pp.max_per_name,
        "pure_play_min_total": pp.pure_play_min_total,
    }
    extra = sp["qtum"] - pp.qtum_weight
    if extra > 1e-9:
        out["bucket_note"] = (
            f"{extra:.1%} of the sleeve couldn't be placed and goes to QTUM: "
            f"{n_pure} eligible pure-play{'s' if n_pure != 1 else ''} at most "
            f"{pp.max_per_name:.0%} each, and {n_adj} adjacent name{'s' if n_adj != 1 else ''} "
            f"at most {pp.adjacent_max_per_name:.0%} each ({pp.adjacent_max_total:.0%} in all). "
            "Pure-play weight never overflows into an adjacent name."
        )
    return out


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
    types = {s: inputs.types.get(s, "pure_play") for s in inputs.sleeve}
    pure_idx = [j for j in sleeve_idx if types[cols[j]] != "adjacent"]
    adj_idx = [j for j in sleeve_idx if types[cols[j]] == "adjacent"]
    for profile in PROFILES:
        pp = config.profiles[profile]
        buckets = (
            Buckets(pure_idx, adj_idx, pp.adjacent_max_total, pp.adjacent_max_per_name)
            if pp.adjacent_max_total is not None and pp.adjacent_max_per_name is not None
            else None
        )
        cands: list[Candidate] = []
        for family in pp.families:  # M16: only the profile's allowed families are backtested
            for q in (pp.qtum_weight,):  # one fixed QTUM weight per profile (§6.5)
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
                    floor=pp.min_per_name,
                    buckets=buckets,
                )
                m = compute_metrics(res.returns, oos_dates, vs, ann=ann, turnovers=res.turnovers)
                qual = qualify(m, bench_metrics[CORE], pp)
                cands.append(Candidate(sid, m, qual, family))
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
        chosen = choose(cands, pp)
        t_now = len(calendar)
        bsum = (
            bucket_summary(
                weights[chosen["strategy_id"]],
                types,
                pp,
                len(eligible(R, t_now, pure_idx, params.min_sessions)),
                len(eligible(R, t_now, adj_idx, params.min_sessions)),
            )
            if chosen["strategy_id"]
            else {}
        )
        profiles_out[profile] = {
            **chosen,
            **bsum,
            "families": list(pp.families),
            "not_allowed": [f for f in FAMILIES if f not in pp.families],
            "rank_metric": pp.rank_metric,
            "limits": {
                "volatility": None if pp.vol_limit_x is None else pp.vol_limit_x * q_vol,
                "max_drawdown": None
                if pp.max_dd_limit_pp is None
                else q_dd + pp.max_dd_limit_pp / 100.0,
            },
            "qtum_weight": pp.qtum_weight,
            "max_per_name": pp.max_per_name,
            "min_per_name": pp.min_per_name,
            "floor": floor_note(R, t_now, sleeve_idx, pp, params)
            if buckets is None
            else bucket_floor_note(R, t_now, buckets, pp, params),
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
    inputs = load_inputs(engine, config.sleeve_types)
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
