"""Read-only queries for the Strategies page. Callers pass the dashboard's `mode=ro` engine."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select

from aether.config import PROFILES
from aether.db.models import strategy_curves, strategy_metrics, strategy_runs, strategy_weights
from aether.market import job_stale, last_ok_finished

STRATEGIES_JOB_MAX_AGE = timedelta(hours=30)
BANNER = "Backtest for reference only. Historical returns are not future gains."
FAMILY_LABELS = {
    "core_equal": "QTUM core + equal-weight sleeve",
    "core_inv_vol": "QTUM core + inverse-volatility sleeve",
    "core_min_var": "QTUM core + minimum-variance sleeve",
    "core_momentum": "QTUM core + top-3 momentum sleeve",
}

# (metric key, column label, format): pct = signed %, pctu = unsigned %, r = ratio, n = integer.
METRIC_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("total_return", "Total return", "pct"),
    ("cagr", "CAGR", "pct"),
    ("volatility", "Volatility", "pctu"),
    ("downside_deviation", "Downside dev.", "pctu"),
    ("max_drawdown", "Max DD", "pctu"),
    ("max_dd_duration", "DD duration (sessions)", "n"),
    ("var95", "VaR95 (daily)", "pctu"),
    ("cvar95", "CVaR95 (daily)", "pctu"),
    ("sharpe", "Sharpe", "r"),
    ("sortino", "Sortino", "r"),
    ("calmar", "Calmar", "r"),
    ("beta_QQQ", "Beta vs QQQ", "r"),
    ("alpha_QQQ", "Alpha vs QQQ", "pct"),
    ("beta_QTUM", "Beta vs QTUM", "r"),
    ("alpha_QTUM", "Alpha vs QTUM", "pct"),
    ("tracking_error_QQQ", "TE vs QQQ", "pctu"),
    ("information_ratio_QQQ", "IR vs QQQ", "r"),
    ("tracking_error_QTUM", "TE vs QTUM", "pctu"),
    ("information_ratio_QTUM", "IR vs QTUM", "r"),
    ("up_capture_QQQ", "Up capture (QQQ)", "r"),
    ("down_capture_QQQ", "Down capture (QQQ)", "r"),
    ("worst_month", "Worst month", "pct"),
    ("pct_positive_months", "% months up", "pctu"),
    ("avg_turnover", "Avg turnover / rebalance", "pctu"),
)


@dataclass(frozen=True)
class StrategyRow:
    strategy_id: str
    kind: str
    profile: str | None
    family: str | None
    qtum_weight: float | None
    metrics: dict[str, Any]
    qualifies: dict[str, Any]


@dataclass(frozen=True)
class ProfileView:
    name: str
    selection: dict[str, Any]
    recommended: StrategyRow | None
    weights: list[tuple[str, float]]
    candidates: list[StrategyRow]


@dataclass(frozen=True)
class StrategiesView:
    run_id: int
    as_of: str
    created_at: str
    summary: dict[str, Any]
    benchmarks: list[StrategyRow]
    profiles: list[ProfileView] = field(default_factory=list)


@dataclass(frozen=True)
class StrategiesFreshness:
    last_ok: str | None
    stale: bool


def freshness(engine: Engine, now: datetime | None = None) -> StrategiesFreshness:
    last = last_ok_finished(engine, "strategies")
    return StrategiesFreshness(
        last, job_stale(last, now or datetime.now(UTC), STRATEGIES_JOB_MAX_AGE)
    )


def latest_run_id(engine: Engine) -> int | None:
    with engine.connect() as conn:
        rid: int | None = conn.execute(
            select(strategy_runs.c.id).order_by(strategy_runs.c.id.desc()).limit(1)
        ).scalar()
    return rid


def strategies_view(engine: Engine) -> StrategiesView | None:
    run_id = latest_run_id(engine)
    if run_id is None:
        return None
    with engine.connect() as conn:
        run = conn.execute(
            select(
                strategy_runs.c.as_of, strategy_runs.c.created_at, strategy_runs.c.summary
            ).where(strategy_runs.c.id == run_id)
        ).one()
        rows = [
            StrategyRow(
                r.strategy_id,
                r.kind,
                r.profile,
                r.family,
                r.qtum_weight,
                json.loads(r.metrics),
                json.loads(r.qualifies),
            )
            for r in conn.execute(
                select(strategy_metrics)
                .where(strategy_metrics.c.run_id == run_id)
                .order_by(strategy_metrics.c.strategy_id)
            )
        ]
        weights: dict[str, list[tuple[str, float]]] = {}
        for sid, sym, w in conn.execute(
            select(
                strategy_weights.c.strategy_id, strategy_weights.c.symbol, strategy_weights.c.weight
            ).where(strategy_weights.c.run_id == run_id)
        ):
            weights.setdefault(sid, []).append((sym, w))
    summary = json.loads(run.summary)
    by_id = {r.strategy_id: r for r in rows}
    profiles = []
    for p in PROFILES:
        sel = summary["profiles"][p]
        sid = sel.get("strategy_id")
        ranking = {s: i for i, s in enumerate(sel.get("ranking", []))}
        cands = sorted(
            (r for r in rows if r.profile == p),
            key=lambda r: (ranking.get(r.strategy_id, len(ranking)), r.strategy_id),
        )
        profiles.append(
            ProfileView(
                p,
                sel,
                by_id.get(sid) if sid else None,
                sorted(weights.get(sid, []), key=lambda x: (-x[1], x[0])) if sid else [],
                cands,
            )
        )
    return StrategiesView(
        run_id,
        run.as_of,
        run.created_at,
        summary,
        [r for r in rows if r.kind == "benchmark"],
        profiles,
    )


def curves_for(engine: Engine, profile: str) -> dict[str, Any]:
    """Equity (rebased to 100) and drawdown series for a profile's recommended strategy vs
    QTUM and QQQ, from the latest run."""
    run_id = latest_run_id(engine)
    if run_id is None:
        return {"as_of": None, "strategy_id": None, "equity": [], "drawdown": []}
    with engine.connect() as conn:
        summary = json.loads(
            conn.execute(
                select(strategy_runs.c.summary).where(strategy_runs.c.id == run_id)
            ).scalar_one()
        )
        sid = summary["profiles"][profile].get("strategy_id")
        wanted = [s for s in (sid, "QTUM", "QQQ") if s]
        pts = {
            s: json.loads(p)
            for s, p in conn.execute(
                select(strategy_curves.c.series_id, strategy_curves.c.points).where(
                    strategy_curves.c.run_id == run_id, strategy_curves.c.series_id.in_(wanted)
                )
            )
        }
    equity_series, dd_series = [], []
    for s in wanted:
        data = pts.get(s, [])
        name = "Recommended" if s == sid else s
        equity_series.append({"name": name, "data": [[d, round(v, 4)] for d, v in data]})
        peak, dd = 0.0, []
        for d, v in data:
            peak = max(peak, v)
            dd.append([d, round((v / peak - 1.0) * 100.0, 4)])
        dd_series.append({"name": name, "data": dd})
    return {
        "as_of": summary["as_of"],
        "strategy_id": sid,
        "equity": equity_series,
        "drawdown": dd_series,
    }
