"""Deterministic daily scorecard per ticker (spec §6.1). No LLM; weights and anchors from
`config/weights.yaml`.

Each component keeps its raw metrics, a score in [-1, 1] (the mean of its available sub-scores, each
a piecewise-linear anchor map), or null with a reason. The total is
100 x sum(w x score) / sum(w) over the components with a score; `coverage` is their share of the
total configured weight. Components:

- fundamentals: TTM revenue growth, cash runway ("not burning" scores +1), EV/Sales;
- dilution: fully diluted shares YoY, warrants + convertible shares as % of common, active ATM /
  shelf;
- signal_momentum: tanh(sum(materiality x direction x confidence x decay) / scale) over
  non-quarantined SIGNAL events, decay = 0.5^(age / half-life);
- risk_load: tanh((same sum over RISK events - open flags x penalty) / scale);
- short_interest: % of shares outstanding, days to cover, change vs the prior report;
- catalyst_position: upcoming catalysts within the horizon, hit rate of resolved ones;
- noise_ratio: NOISE / all classified events in the window (plus a hype flag);
- price_context: drawdown from the 52-week high and 90-day performance vs QTUM (realized
  volatility and 30-day IV are shown, not scored);
- market_reaction: mean z5 of recent complete reactions (weight 0 by default; context only).

QTUM (the ETF) has no fundamentals, dilution or short-interest component.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date, timedelta
from statistics import mean
from typing import Any

import numpy as np
from sqlalchemy import Connection, Engine, select

from aether.config import SLEEVE_TYPES, Rubric, ScorecardParams, WeightsConfig
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    catalysts,
    event_classifications,
    event_reactions,
    event_tickers,
    events,
    options_snapshots,
    scorecards,
    short_interest,
    tickers,
)
from aether.db.types import utcnow_iso
from aether.risk.flags import Flag, open_flags
from aether.runs import JobResult
from aether.score import fundamentals as fnd
from aether.score.prices import PriceSeries, load_series, upto

ALGO_VERSION = "m9.1"
DP = 6


def _r(x: float | None, dp: int = DP) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, dp) + 0.0


def _component(subs: dict[str, float | None], metrics: dict[str, Any], why: str) -> dict[str, Any]:
    vals = [v for v in subs.values() if v is not None]
    return {
        "score": _r(mean(vals)) if vals else None,
        "subscores": {k: _r(v) for k, v in subs.items()},
        "metrics": metrics,
        "reason": None if vals else why,
    }


# --------------------------------------------------------------------------- components (pure)


def fundamentals_component(snap: fnd.Snapshot, p: ScorecardParams) -> dict[str, Any]:
    a = p.anchors
    runway: float | None
    if snap.not_burning:
        runway = 1.0
    elif snap.runway_months is not None:
        runway = a["runway_months"].score(snap.runway_months)
    else:
        runway = None
    subs = {
        "revenue_growth": (
            None if snap.revenue_growth is None else a["revenue_growth"].score(snap.revenue_growth)
        ),
        "runway": runway,
        "ev_sales": None if snap.ev_sales is None else a["ev_sales"].score(snap.ev_sales),
    }
    return _component(subs, {"snapshot": snap.to_json()}, "no tagged fundamentals")


def dilution_component(
    snap: fnd.Snapshot, flags: Sequence[Flag], p: ScorecardParams
) -> dict[str, Any]:
    a = p.anchors
    kinds = {f.kind for f in flags}
    capacity: float | None = 0.0
    if "active_atm" in kinds:
        capacity = p.atm_active_score
    elif "active_shelf" in kinds:
        capacity = p.shelf_active_score
    yoy = snap.fd_yoy.get("value")
    subs = {
        "fd_yoy": None if yoy is None else a["fd_yoy"].score(yoy),
        "instruments_pct": (
            None
            if snap.instruments_pct is None
            else a["instruments_pct"].score(snap.instruments_pct)
        ),
        "shelf_atm": capacity,
    }
    metrics = {
        "fd_yoy": _r(yoy),
        "fd_yoy_reason": snap.fd_yoy.get("reason"),
        "instruments_pct": _r(snap.instruments_pct),
        "active_atm": "active_atm" in kinds,
        "active_shelf": "active_shelf" in kinds,
    }
    return _component(subs, metrics, "no share data")


def decayed_sum(rows: Sequence[dict[str, Any]], as_of: date, half_life: float) -> tuple[float, int]:
    total = 0.0
    for r in rows:
        age = max(0.0, (as_of - date.fromisoformat(r["published_at"][:10])).days)
        total += r["materiality"] * r["direction"] * r["confidence"] * 0.5 ** (age / half_life)
    return total, len(rows)


def momentum_component(
    sig: Sequence[dict[str, Any]], as_of: date, p: ScorecardParams
) -> dict[str, Any]:
    s, n = decayed_sum(sig, as_of, p.half_life_days)
    score = math.tanh(s / p.momentum_scale) if n else None
    return _component(
        {"momentum": score}, {"sum": _r(s), "events": n}, "no SIGNAL events in the lookback"
    )


def risk_component(
    risk: Sequence[dict[str, Any]], flags: Sequence[Flag], as_of: date, p: ScorecardParams
) -> dict[str, Any]:
    s, n = decayed_sum(risk, as_of, p.half_life_days)
    total = s - len(flags) * p.open_flag_penalty
    return _component(
        {"risk": math.tanh(total / p.risk_scale)},
        {
            "event_sum": _r(s),
            "events": n,
            "open_flags": sorted(f.kind for f in flags),
            "total": _r(total),
        },
        "",
    )


def short_component(rows: Sequence[Any], p: ScorecardParams) -> dict[str, Any]:
    a = p.anchors
    if not rows:
        return _component({}, {}, "no FINRA short-interest report")
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    change = (
        cur.pct_shares_out - prev.pct_shares_out
        if prev is not None and cur.pct_shares_out is not None and prev.pct_shares_out is not None
        else None
    )
    subs = {
        "pct_shares_out": (
            None
            if cur.pct_shares_out is None
            else a["short_pct_shares_out"].score(cur.pct_shares_out / 100.0)
        ),
        "days_to_cover": (
            None if cur.days_to_cover is None else a["days_to_cover"].score(cur.days_to_cover)
        ),
        "change_pp": None if change is None else a["short_change_pp"].score(change),
    }
    metrics = {
        "settlement_date": cur.settlement_date,
        "pct_shares_out": _r(cur.pct_shares_out),
        "days_to_cover": _r(cur.days_to_cover),
        "change_pp": _r(change),
    }
    return _component(subs, metrics, "report has no usable figures")


def catalyst_component(upcoming: int, hits: int, slips: int, p: ScorecardParams) -> dict[str, Any]:
    a = p.anchors
    rate = hits / (hits + slips) if hits + slips else None
    subs = {
        "upcoming": a["catalysts_upcoming"].score(upcoming),
        "hit_rate": None if rate is None else a["catalyst_hit_rate"].score(rate),
    }
    return _component(
        subs,
        {"upcoming": upcoming, "hits": hits, "slips": slips, "hit_rate": _r(rate)},
        "",
    )


def noise_component(
    classes: Sequence[str], ret_30: float | None, p: ScorecardParams
) -> dict[str, Any]:
    n = len(classes)
    noise = sum(c == "NOISE" for c in classes)
    ratio = noise / n if n else None
    hype = bool(ratio is not None and ratio > p.hype_noise_ratio and (ret_30 or 0) > 0)
    score = (
        p.anchors["noise_ratio"].score(ratio)
        if ratio is not None and n >= p.noise_min_events
        else None
    )
    return _component(
        {"noise_ratio": score},
        {"events": n, "noise": noise, "ratio": _r(ratio), "return_30d": _r(ret_30), "hype": hype},
        f"fewer than {p.noise_min_events} classified events in {p.noise_window_days} days",
    )


def _level_on_or_before(s: PriceSeries, d: str) -> float | None:
    idx = [i for i, x in enumerate(s.days) if x <= d]
    return s.levels[idx[-1]] if idx else None


def price_metrics(
    s: PriceSeries, qtum: PriceSeries | None, as_of: date, iv30: float | None
) -> dict[str, Any]:
    out: dict[str, Any] = {"iv_30": _r(iv30)}
    if not s.days:
        return out
    last = s.days[-1]
    year = (as_of - timedelta(days=365)).isoformat()
    hi = max(c for d, c in zip(s.days, s.closes, strict=True) if d >= year)
    out["drawdown_52w"] = _r(s.closes[-1] / hi - 1.0)
    rets = np.diff(np.array(s.levels)) / np.array(s.levels[:-1])
    for n in (30, 90):
        if len(rets) >= n:
            out[f"vol_{n}d"] = _r(float(np.std(rets[-n:], ddof=1) * math.sqrt(252)))
    for days in (30, 90):
        start = (as_of - timedelta(days=days)).isoformat()
        base = _level_on_or_before(s, start)
        out[f"return_{days}d"] = _r(s.levels[-1] / base - 1.0) if base else None
    if qtum is not None and qtum.days and out.get("return_90d") is not None:
        qb = _level_on_or_before(qtum, (as_of - timedelta(days=90)).isoformat())
        if qb:
            out["rel_perf_90d"] = _r(out["return_90d"] - (qtum.levels[-1] / qb - 1.0))
    out["last"] = last
    return out


def price_component(m: dict[str, Any], p: ScorecardParams) -> dict[str, Any]:
    a = p.anchors
    dd, rel = m.get("drawdown_52w"), m.get("rel_perf_90d")
    subs = {
        "drawdown_52w": None if dd is None else a["drawdown_52w"].score(dd),
        "rel_perf_90d": None if rel is None else a["rel_perf_90d"].score(rel),
    }
    return _component(subs, m, "no price history")


def reaction_component(z5s: Sequence[float], p: ScorecardParams) -> dict[str, Any]:
    mz = mean(z5s) if z5s else None
    return _component(
        {"mean_z5": None if mz is None else p.anchors["mean_z5"].score(mz)},
        {"n": len(z5s), "mean_z5": _r(mz)},
        f"no complete, non-confounded reactions in {p.reaction_window_days} days",
    )


def total(
    components: dict[str, dict[str, Any]], weights: dict[str, float]
) -> tuple[float | None, float]:
    num = den = 0.0
    all_w = sum(w for k, w in weights.items() if k in components and w > 0)
    for k, c in components.items():
        w = weights[k]
        if w > 0 and c["score"] is not None:
            num += w * c["score"]
            den += w
    tot = _r(100.0 * num / den, 4) if den else None
    return tot, (_r(den / all_w, 6) or 0.0) if all_w else 0.0


# --------------------------------------------------------------------------- reads


def _events(conn: Connection, sym: str, start: str, end: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(
            events.c.published_at,
            event_classifications.c["class"],
            event_classifications.c.materiality,
            event_classifications.c.direction,
            event_classifications.c.confidence,
            event_tickers.c.direction.label("ticker_direction"),
        )
        .join(event_tickers, event_tickers.c.event_id == events.c.id)
        .join(event_classifications, event_classifications.c.event_id == events.c.id)
        .where(
            event_tickers.c.symbol == sym,
            events.c.quarantined == 0,
            events.c.published_at >= start,
            events.c.published_at <= end,
        )
        .order_by(events.c.published_at, events.c.id)
    ).all()
    return [
        {
            "published_at": r.published_at,
            "class": r._mapping["class"],
            "materiality": r.materiality,
            "direction": r.ticker_direction if r.ticker_direction is not None else r.direction,
            "confidence": r.confidence,
        }
        for r in rows
    ]


def compute_scorecard(
    conn: Connection,
    sym: str,
    typ: str,
    as_of: date,
    w: WeightsConfig,
    flags: Sequence[Flag],
    series: dict[str, PriceSeries],
) -> dict[str, Any]:
    p = w.scorecard
    end = f"{as_of.isoformat()}T23:59:59Z"
    look = f"{(as_of - timedelta(days=p.event_lookback_days)).isoformat()}T00:00:00Z"
    evs = _events(conn, sym, look, end)
    comps: dict[str, dict[str, Any]] = {}
    if typ in SLEEVE_TYPES:
        snap = fnd.snapshot(conn, sym, as_of)
        comps["fundamentals"] = fundamentals_component(snap, p)
        comps["dilution"] = dilution_component(snap, flags, p)
        si = conn.execute(
            select(short_interest)
            .where(
                short_interest.c.symbol == sym,
                short_interest.c.settlement_date <= as_of.isoformat(),
            )
            .order_by(short_interest.c.settlement_date.desc())
            .limit(2)
        ).all()
        comps["short_interest"] = short_component(si, p)
    comps["signal_momentum"] = momentum_component(
        [e for e in evs if e["class"] == "SIGNAL"], as_of, p
    )
    comps["risk_load"] = risk_component([e for e in evs if e["class"] == "RISK"], flags, as_of, p)

    horizon = (as_of + timedelta(days=p.catalyst_horizon_days)).isoformat()
    cats = conn.execute(
        select(catalysts.c.status, catalysts.c.window_start).where(catalysts.c.symbol == sym)
    ).all()
    upcoming = sum(c.status == "upcoming" and c.window_start <= horizon for c in cats)
    comps["catalyst_position"] = catalyst_component(
        upcoming, sum(c.status == "hit" for c in cats), sum(c.status == "slipped" for c in cats), p
    )

    iv = conn.execute(
        select(options_snapshots.c.metrics)
        .where(options_snapshots.c.symbol == sym, options_snapshots.c.d <= as_of.isoformat())
        .order_by(options_snapshots.c.d.desc())
        .limit(1)
    ).scalar()
    iv30 = json.loads(iv).get("atm_iv_30") if iv else None
    q = upto(series["QTUM"], as_of) if sym != "QTUM" and "QTUM" in series else None
    pm = price_metrics(upto(series[sym], as_of), q, as_of, iv30)
    comps["price_context"] = price_component(pm, p)

    nstart = f"{(as_of - timedelta(days=p.noise_window_days)).isoformat()}T00:00:00Z"
    comps["noise_ratio"] = noise_component(
        [e["class"] for e in evs if e["published_at"] >= nstart], pm.get("return_30d"), p
    )

    rstart = (as_of - timedelta(days=p.reaction_window_days)).isoformat()
    z5 = [
        z
        for (z,) in conn.execute(
            select(event_reactions.c.z_5).where(
                event_reactions.c.symbol == sym,
                event_reactions.c.status == "complete",
                event_reactions.c.approx_time == 0,
                event_reactions.c.t0 >= rstart,
                event_reactions.c.z_5.is_not(None),
            )
        )
    ]
    comps["market_reaction"] = reaction_component(z5, p)

    for k, c in comps.items():
        c["weight"] = p.weights[k]
    tot, coverage = total(comps, p.weights)
    missing = sorted(k for k, c in comps.items() if c["score"] is None and p.weights[k] > 0)
    return {
        "components": {k: comps[k] for k in sorted(comps)},
        "total": tot,
        "coverage": coverage,
        "missing": missing,
    }


def run_scorecards(
    engine: Engine, w: WeightsConfig, rubric: Rubric, as_of: date | None = None
) -> JobResult:
    with engine.connect() as conn:
        syms = conn.execute(
            select(tickers.c.symbol, tickers.c.type)
            .where(tickers.c.type.in_((*SLEEVE_TYPES, "etf")), tickers.c.active == 1)
            .order_by(tickers.c.symbol)
        ).all()
        series = load_series(conn, [s for s, _ in syms] + ["QTUM"])
    if as_of is None:
        last = series["QTUM"].last
        if last is None:
            return JobResult(warning="no QTUM prices yet")
        as_of = date.fromisoformat(last)
    # Separate connections: read them before opening ours (rw connections begin IMMEDIATE).
    pure = [s for s, t in syms if t in SLEEVE_TYPES]
    all_flags = open_flags(engine, rubric.risk_flags, as_of, pure, rubric.short_interest)
    with engine.connect() as conn:
        rows = []
        for sym, typ in syms:
            flags = [f for f in all_flags if f.symbol == sym]
            sc = compute_scorecard(conn, sym, typ, as_of, w, flags, series)
            payload = {"components": sc["components"], "missing": sc["missing"]}
            comps = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(
                json.dumps(
                    {"algo": ALGO_VERSION, "c": payload, "w": w.scorecard.model_dump(mode="json")},
                    sort_keys=True,
                ).encode()
            ).digest()
            rows.append(
                {
                    "symbol": sym,
                    "as_of": as_of.isoformat(),
                    "components": comps,
                    "total": sc["total"],
                    "coverage": sc["coverage"],
                    "input_hash": digest,
                    "created_at": utcnow_iso(),
                }
            )
        existing = {
            r.symbol: r.input_hash
            for r in conn.execute(
                select(scorecards.c.symbol, scorecards.c.input_hash).where(
                    scorecards.c.as_of == as_of.isoformat()
                )
            )
        }
    todo = [r for r in rows if existing.get(r["symbol"]) != r["input_hash"]]
    if todo:
        with write_tx(engine) as conn:
            upsert(conn, scorecards, todo, key_cols=["symbol", "as_of"])
    return JobResult(rows_written=len(todo))
