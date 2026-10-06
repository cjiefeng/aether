"""Conclusion track record (spec §6.4) and the overlay's value-added check (spec §6.6.1 layer 3).
Deterministic, no LLM.

**Outcomes.** For every stored conclusion that isn't a held update (held updates don't count as new
calls), and each horizon (1, 3, 6, 12, 24, 36 months):
- start = the last close on or before the conclusion's `as_of`; end = the last close on or before
  `as_of` + N calendar months; both on total-return levels (`score/prices.py`);
- excess return = the ticker's return minus the benchmark's over that window. The benchmark is
  QTUM, QQQ for QTUM's own stance. The theme tilt compares the equal-weight pure-play basket
  (the names with a close at the start) with QTUM;
- the row stays `pending` until the benchmark has a session on or after the end date;
- hits: ACCUMULATE > 0, TRIM/AVOID < 0, HOLD |excess| < the horizon's band (theme: PURE_PLAYS > 0,
  QTUM < 0, NEUTRAL inside the band);
- baselines on the same window: "always HOLD" (`hold_hit`) and momentum (the sign of the
  ticker's excess return over the previous `momentum_lookback_days`, judged as ACCUMULATE/AVOID).

Every non-held conclusion counts as a call (owner decision 2026-10-06), so weekly reaffirmations
of the same stance have overlapping windows and their hits are correlated. The UI says so.

**Proven.** A ticker's stance is proven once it has at least `min_mature_calls` complete 6-month
calls **and** its 6-month hit rate beats both baselines. Until then the overlay clamps its
multiplier (§6.6.1).

**Overlay outcomes.** Each `profile_targets` publish is held as two buy-and-hold paper portfolios
(its base weights and its published weights) from the last close on or before the publish date.
"""

from __future__ import annotations

import bisect
import calendar
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from statistics import mean, median
from typing import Any

from sqlalchemy import Connection, Engine, select

from aether.config import HORIZONS, TrackRecordParams
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    conclusion_outcomes,
    conclusions,
    overlay_outcomes,
    profile_targets,
    tickers,
)
from aether.db.types import utcnow_iso
from aether.runs import JobResult
from aether.score.prices import PriceSeries, load_series

HORIZON_MONTHS = {"1m": 1, "3m": 3, "6m": 6, "12m": 12, "24m": 24, "36m": 36}
PROVEN_HORIZON = "6m"
OVERLAY_VERDICT_PUBLISHES = 12
CORE = "QTUM"
DP = 8


def _r(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, DP) + 0.0


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def level_on_or_before(s: PriceSeries, day: str) -> tuple[str, float] | None:
    i = bisect.bisect_right(s.days, day)
    return None if i == 0 else (s.days[i - 1], s.levels[i - 1])


def window_return(s: PriceSeries, start: str, end: str) -> float | None:
    a, b = level_on_or_before(s, start), level_on_or_before(s, end)
    if a is None or b is None or a[1] <= 0:
        return None
    return b[1] / a[1] - 1.0


def basket_return(series: Sequence[PriceSeries], start: str, end: str) -> float | None:
    """Equal-weight buy-and-hold of the names with a close at the start."""
    rets = [r for s in series if (r := window_return(s, start, end)) is not None]
    return mean(rets) if rets else None


def is_hit(stance: str, excess: float, band: float) -> bool:
    if stance in ("ACCUMULATE", "PURE_PLAYS"):
        return excess > 0
    if stance in ("TRIM", "AVOID", "QTUM"):
        return excess < 0
    return abs(excess) < band  # HOLD / NEUTRAL


# --------------------------------------------------------------------------- outcomes


@dataclass(frozen=True)
class CallRef:
    id: int
    kind: str
    symbol: str | None
    as_of: str
    stance: str


def benchmark_for(c: CallRef) -> str:
    return "QQQ" if c.symbol == CORE else CORE


def _subject_return(
    c: CallRef, series: Mapping[str, PriceSeries], basket: Sequence[str], start: str, end: str
) -> float | None:
    if c.kind == "theme":
        return basket_return([series[s] for s in basket if s in series], start, end)
    s = series.get(c.symbol or "")
    return None if s is None else window_return(s, start, end)


def outcome_rows(
    calls: Iterable[CallRef],
    series: Mapping[str, PriceSeries],
    basket: Sequence[str],
    p: TrackRecordParams,
) -> list[dict[str, Any]]:
    """Pure: the outcome row of every (call, horizon)."""
    now = utcnow_iso()
    out = []
    for c in calls:
        bench = benchmark_for(c)
        b = series.get(bench)
        start_day = c.as_of
        start = level_on_or_before(b, start_day) if b is not None else None
        mom_stance = None
        if start is not None and b is not None:
            back = date.fromisoformat(start[0]) - timedelta(days=p.momentum_lookback_days)
            sub = _subject_return(c, series, basket, back.isoformat(), start[0])
            bret = window_return(b, back.isoformat(), start[0])
            if sub is not None and bret is not None and back.isoformat() >= (b.first or ""):
                up = sub - bret > 0
                if c.kind == "theme":
                    mom_stance = "PURE_PLAYS" if up else "QTUM"
                else:
                    mom_stance = "ACCUMULATE" if up else "AVOID"
        for h in HORIZONS:
            end_d = add_months(date.fromisoformat(c.as_of), HORIZON_MONTHS[h]).isoformat()
            row: dict[str, Any] = {
                "conclusion_id": c.id,
                "horizon": h,
                "benchmark": bench if c.kind == "ticker" else f"basket vs {CORE}",
                "start_d": start[0] if start else None,
                "end_d": end_d,
                "excess_return": None,
                "hit": None,
                "hold_hit": None,
                "momentum_stance": mom_stance,
                "momentum_hit": None,
                "status": "pending",
                "computed_at": now,
            }
            mature = b is not None and start is not None and (b.last or "") >= end_d
            if mature:
                assert start is not None and b is not None
                sub = _subject_return(c, series, basket, start[0], end_d)
                bret = window_return(b, start[0], end_d)
                if sub is not None and bret is not None:
                    x = sub - bret
                    band = p.hold_band[h]
                    row.update(
                        excess_return=_r(x),
                        hit=int(is_hit(c.stance, x, band)),
                        hold_hit=int(abs(x) < band),
                        momentum_hit=(
                            None if mom_stance is None else int(is_hit(mom_stance, x, band))
                        ),
                        status="complete",
                    )
            out.append(row)
    return out


def _calls(conn: Connection) -> list[CallRef]:
    return [
        CallRef(r.id, r.kind, r.symbol, r.as_of, r.stance)
        for r in conn.execute(
            select(
                conclusions.c.id,
                conclusions.c.kind,
                conclusions.c.symbol,
                conclusions.c.as_of,
                conclusions.c.stance,
            )
            .where(conclusions.c.held == 0)
            .order_by(conclusions.c.id)
        )
    ]


def pure_plays(conn: Connection) -> list[str]:
    return list(
        conn.execute(
            select(tickers.c.symbol)
            .where(tickers.c.type == "pure_play", tickers.c.active == 1)
            .order_by(tickers.c.symbol)
        ).scalars()
    )


_COMPARE = (
    "start_d",
    "end_d",
    "excess_return",
    "hit",
    "hold_hit",
    "momentum_stance",
    "momentum_hit",
    "status",
    "benchmark",
)


def _changed(old: Mapping[str, Any] | None, new: Mapping[str, Any], keys: Sequence[str]) -> bool:
    return old is None or any(old[k] != new[k] for k in keys)


# --------------------------------------------------------------------------- overlay outcomes


def portfolio_return(
    weights: Mapping[str, float], series: Mapping[str, PriceSeries], start: str, end: str
) -> float | None:
    """Buy-and-hold return of a weight vector; None if any held name has no start close."""
    total = 0.0
    for sym, w in weights.items():
        if w <= 0:
            continue
        s = series.get(sym)
        r = None if s is None else window_return(s, start, end)
        if r is None:
            return None
        total += w * r
    return total


def overlay_rows(
    publishes: Iterable[Mapping[str, Any]], series: Mapping[str, PriceSeries]
) -> list[dict[str, Any]]:
    now = utcnow_iso()
    core = series.get(CORE)
    out = []
    for pub in publishes:
        start = level_on_or_before(core, pub["as_of"]) if core is not None else None
        for h in HORIZONS:
            end_d = add_months(date.fromisoformat(pub["as_of"]), HORIZON_MONTHS[h]).isoformat()
            row: dict[str, Any] = {
                "profile": pub["profile"],
                "as_of": pub["as_of"],
                "horizon": h,
                "start_d": start[0] if start else None,
                "end_d": end_d,
                "base_return": None,
                "adjusted_return": None,
                "status": "pending",
                "computed_at": now,
            }
            if core is not None and start is not None and (core.last or "") >= end_d:
                base = portfolio_return(pub["base"], series, start[0], end_d)
                adj = portfolio_return(pub["published"], series, start[0], end_d)
                if base is not None and adj is not None:
                    row.update(base_return=_r(base), adjusted_return=_r(adj), status="complete")
            out.append(row)
    return out


# --------------------------------------------------------------------------- job


def run_track_record(engine: Engine, p: TrackRecordParams) -> JobResult:
    with engine.connect() as conn:
        calls = _calls(conn)
        pubs = [
            {
                "profile": r.profile,
                "as_of": r.as_of,
                "base": json.loads(r.base_weights),
                "published": json.loads(r.published_weights),
            }
            for r in conn.execute(
                select(
                    profile_targets.c.profile,
                    profile_targets.c.as_of,
                    profile_targets.c.base_weights,
                    profile_targets.c.published_weights,
                ).order_by(profile_targets.c.as_of, profile_targets.c.profile)
            )
        ]
        basket = pure_plays(conn)
        syms = {CORE, "QQQ", *basket}
        syms |= {c.symbol for c in calls if c.symbol}
        for pub in pubs:
            syms |= set(pub["base"]) | set(pub["published"])
        series = load_series(conn, syms)
        old_c = {
            (r.conclusion_id, r.horizon): dict(r._mapping)
            for r in conn.execute(select(conclusion_outcomes))
        }
        old_o = {
            (r.profile, r.as_of, r.horizon): dict(r._mapping)
            for r in conn.execute(select(overlay_outcomes))
        }
    c_rows = [
        r
        for r in outcome_rows(calls, series, basket, p)
        if _changed(old_c.get((r["conclusion_id"], r["horizon"])), r, _COMPARE)
    ]
    o_rows = [
        r
        for r in overlay_rows(pubs, series)
        if _changed(
            old_o.get((r["profile"], r["as_of"], r["horizon"])),
            r,
            ("start_d", "end_d", "base_return", "adjusted_return", "status"),
        )
    ]
    if c_rows or o_rows:
        with write_tx(engine) as conn:
            if c_rows:
                upsert(conn, conclusion_outcomes, c_rows, key_cols=["conclusion_id", "horizon"])
            if o_rows:
                upsert(conn, overlay_outcomes, o_rows, key_cols=["profile", "as_of", "horizon"])
    return JobResult(rows_written=len(c_rows) + len(o_rows))


# --------------------------------------------------------------------------- summaries (reads)


def _rate(xs: Sequence[int | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return _r(sum(vals) / len(vals)) if vals else None


def summarize(rows: Sequence[Mapping[str, Any]], p: TrackRecordParams) -> dict[str, Any]:
    """Pure. `rows`: complete outcome rows joined with their conclusion (`stance`, `confidence`,
    `symbol`, `kind`). Per horizon: n, hit rate, mean/median excess, both baselines."""
    by_h: dict[str, list[Mapping[str, Any]]] = {h: [] for h in HORIZONS}
    for r in rows:
        if r["status"] == "complete":
            by_h[r["horizon"]].append(r)
    out: dict[str, Any] = {}
    for h, rs in by_h.items():
        xs = [float(r["excess_return"]) for r in rs]
        out[h] = {
            "n": len(rs),
            "hit_rate": _rate([r["hit"] for r in rs]),
            "mean_excess": _r(mean(xs)) if xs else None,
            "median_excess": _r(median(xs)) if xs else None,
            "hold_rate": _rate([r["hold_hit"] for r in rs]),
            "momentum_rate": _rate([r["momentum_hit"] for r in rs]),
        }
    return out


def calibration(
    rows: Sequence[Mapping[str, Any]], edges: Sequence[float], horizon: str = PROVEN_HORIZON
) -> dict[str, Any]:
    """Confidence buckets (predicted vs realized hit rate) and the Brier score at one horizon."""
    rs = [r for r in rows if r["status"] == "complete" and r["horizon"] == horizon]
    buckets = []
    for i, (lo, hi) in enumerate(pairwise(edges)):
        last = i == len(edges) - 2
        sel = [r for r in rs if lo <= r["confidence"] < hi or (last and r["confidence"] == hi)]
        buckets.append(
            {
                "lo": lo,
                "hi": hi,
                "n": len(sel),
                "predicted": _r(mean(r["confidence"] for r in sel)) if sel else None,
                "realized": _rate([r["hit"] for r in sel]),
            }
        )
    brier = _r(mean((r["confidence"] - r["hit"]) ** 2 for r in rs)) if rs else None
    return {"horizon": horizon, "buckets": buckets, "brier": brier, "n": len(rs)}


@dataclass(frozen=True)
class Standing:
    """A ticker's (or the theme's) track-record status at the proven horizon."""

    n: int
    hit_rate: float | None
    hold_rate: float | None
    momentum_rate: float | None
    proven: bool
    label: str  # "No track record yet." / "n too small …" / "beats both baselines" / …


def standing(rows: Sequence[Mapping[str, Any]], p: TrackRecordParams) -> Standing:
    s = summarize(rows, p)[PROVEN_HORIZON]
    n = s["n"]
    hr, hold, mom = s["hit_rate"], s["hold_rate"], s["momentum_rate"]
    if n == 0:
        return Standing(0, None, None, None, False, "No track record yet.")
    if n < p.min_mature_calls:
        return Standing(
            n,
            hr,
            hold,
            mom,
            False,
            f"n too small (<{p.min_mature_calls} mature calls): treat stance as unproven",
        )
    beats = hr is not None and all(b is None or hr > b for b in (hold, mom))
    label = "beats both baselines" if beats else "does not beat the baselines: unproven"
    return Standing(n, hr, hold, mom, beats, label)


def outcome_rows_for(
    conn: Connection, symbol: str | None = None, kind: str = "ticker"
) -> list[dict[str, Any]]:
    q = (
        select(
            conclusion_outcomes,
            conclusions.c.stance,
            conclusions.c.confidence,
            conclusions.c.symbol,
            conclusions.c.kind,
            conclusions.c.as_of,
        )
        .join(conclusions, conclusions.c.id == conclusion_outcomes.c.conclusion_id)
        .where(conclusions.c.kind == kind)
        .order_by(conclusions.c.id, conclusion_outcomes.c.horizon)
    )
    if symbol is not None:
        q = q.where(conclusions.c.symbol == symbol)
    return [dict(r._mapping) for r in conn.execute(q)]


def standings(conn: Connection, p: TrackRecordParams) -> dict[str, Standing]:
    """Per ticker symbol (and '$THEME' for the theme tilt)."""
    rows = outcome_rows_for(conn)
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["symbol"], []).append(r)
    out = {s: standing(rs, p) for s, rs in by.items()}
    out["$THEME"] = standing(outcome_rows_for(conn, kind="theme"), p)
    return out


def overlay_value(conn: Connection) -> dict[str, Any]:
    """Layer 3 per profile: per-horizon mean returns, and the verdict once 12 monthly publishes
    have a complete 1-month outcome (chained 1-month returns, adjusted vs base)."""
    rows = conn.execute(
        select(overlay_outcomes, profile_targets.c.trigger)
        .join(
            profile_targets,
            (profile_targets.c.profile == overlay_outcomes.c.profile)
            & (profile_targets.c.as_of == overlay_outcomes.c.as_of),
        )
        .order_by(overlay_outcomes.c.profile, overlay_outcomes.c.as_of)
    ).all()
    out: dict[str, Any] = {}
    for r in rows:
        prof = out.setdefault(r.profile, {"horizons": {}, "monthly_1m": []})
        if r.status != "complete":
            continue
        h = prof["horizons"].setdefault(r.horizon, {"n": 0, "base": [], "adjusted": []})
        h["n"] += 1
        h["base"].append(r.base_return)
        h["adjusted"].append(r.adjusted_return)
        if r.horizon == "1m" and r.trigger == "monthly":
            prof["monthly_1m"].append((r.base_return, r.adjusted_return))
    for prof in out.values():
        for h in prof["horizons"].values():
            h["mean_base"] = _r(mean(h.pop("base")))
            h["mean_adjusted"] = _r(mean(h.pop("adjusted")))
        m = prof.pop("monthly_1m")
        prof["monthly_publishes"] = len(m)
        if len(m) >= OVERLAY_VERDICT_PUBLISHES:
            base = math.prod(1 + b for b, _ in m) - 1
            adj = math.prod(1 + a for _, a in m) - 1
            prof["chained_base"], prof["chained_adjusted"] = _r(base), _r(adj)
            prof["verdict"] = (
                "The overlay-adjusted targets have beaten the base targets."
                if adj > base
                else "The overlay-adjusted targets have not beaten the base targets: consider "
                "setting overlay.enabled: false."
            )
        else:
            prof["verdict"] = (
                f"Too early: {len(m)} of {OVERLAY_VERDICT_PUBLISHES} monthly publishes have a "
                "1-month result."
            )
    return out
