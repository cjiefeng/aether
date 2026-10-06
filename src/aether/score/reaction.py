"""Event-reaction check (spec §6.3): how each stock moved after each classified event, relative to
the theme. Deterministic; no LLM. Reads first, then one short `write_tx`.

Per (non-quarantined, classified event) x (affected pure-play or QTUM):
1. t0 = the first NYSE session whose close is after `published_at` (half-days included).
2. Benchmark: QTUM for pure-plays, QQQ for QTUM's own events.
3. Market model on total-return daily returns over the `estimation_window` sessions ending at
   t0-1: beta = cov/var, residual sigma (n-2 dof). Fewer than `min_sessions` returns -> beta = 1
   (flagged) and sigma = std(r_stock - r_bench).
4. AR_t = r_stock,t - beta * r_bench,t; CAR over [t0,t0+1], [t0,t0+5], [t0,t0+20]. A window is
   filled once both series have a bar on its last session; until then the row is `pending`.
5. z = CAR / (sigma * sqrt(n)), n = sessions in the window.
6. Abnormal volume = volume at t0 / median volume of the previous `volume_window` bars.
7. Reversal ratio = CAR[0,20] / CAR[0,1] (null when |CAR[0,1]| is below `reversal_min_abs_car1`).
8. Confounded: another event on the same ticker with materiality >= `confound_min_materiality`,
   an earnings release, or a RISK EDGAR filing, anchored within [t0-1, t0+5].

`approx_time` marks events whose timestamp is only approximate: research results dated by
retrieval time, or by a date-only `page_age` (stored at 12:00 UTC). They are computed and shown but
excluded from calibration (owner decision 2026-10-06).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

import numpy as np
from sqlalchemy import Connection, Engine, select

from aether import nyse
from aether.config import ReactionParams
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    event_classifications,
    event_reactions,
    event_tickers,
    events,
    tickers,
)
from aether.db.types import utcnow_iso
from aether.portfolio.total_return import align, simple_returns
from aether.runs import JobResult
from aether.score.prices import PriceSeries, load_series

WINDOWS = (1, 5, 20)
VALUE_COLS = (
    "t0",
    "benchmark",
    "beta",
    "sigma_resid",
    "beta_fallback",
    "car_1",
    "car_5",
    "car_20",
    "z_1",
    "z_5",
    "z_20",
    "ret_raw_1",
    "abn_volume",
    "reversal_ratio",
    "status",
    "confounders",
    "approx_time",
    "note",
)
DP = 10


def _r(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, DP) + 0.0


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def anchor(published_at: str) -> date | None:
    return nyse.first_session_closing_after(parse_ts(published_at))


def is_approx_time(origin: str, raw: dict[str, Any] | None, published_at: str) -> bool:
    if origin != "web_search":
        return False
    src = (raw or {}).get("date_source")
    return src == "retrieved" or (src == "page_age" and published_at.endswith("T12:00:00Z"))


# --------------------------------------------------------------------------- pure maths


def compute(
    stock: PriceSeries, bench: PriceSeries, t0: date, params: ReactionParams
) -> dict[str, Any]:
    """Reaction metrics for one event anchored at `t0`. Returns the value columns except status,
    confounders, approx_time and benchmark; `filled` says whether [t0, t0+20] is complete and
    `no_data` carries a reason when nothing can be computed."""
    out: dict[str, Any] = {
        "t0": t0.isoformat(),
        "beta": None,
        "sigma_resid": None,
        "beta_fallback": 0,
        **{f"car_{k}": None for k in WINDOWS},
        **{f"z_{k}": None for k in WINDOWS},
        "ret_raw_1": None,
        "abn_volume": None,
        "reversal_ratio": None,
        "note": None,
        "filled": False,
        "no_data": None,
    }
    t0s = t0.isoformat()
    if not stock.days or t0s < stock.days[0]:
        out["no_data"] = "no price history at t0"
        return out
    if stock.last is None or bench.last is None or t0s > min(stock.last, bench.last):
        out["note"] = "waiting for the t0 bar"
        return out
    if t0s not in set(stock.days):
        out["no_data"] = "no bar at t0"
        return out
    start = date.fromisoformat(max(stock.days[0], bench.days[0]) if bench.days else stock.days[0])
    end = date.fromisoformat(max(stock.last, bench.last))
    cal = [d.isoformat() for d in nyse.sessions(min(start, t0), end)]
    i0 = cal.index(t0s)
    ls = align(list(zip(stock.days, stock.levels, strict=True)), cal)
    lb = align(list(zip(bench.days, bench.levels, strict=True)), cal)
    rs, rb = simple_returns(ls), simple_returns(lb)
    last_filled = max(i for i, d in enumerate(cal) if d <= min(stock.last, bench.last))

    lo = max(1, i0 - params.estimation_window)
    est = [i for i in range(lo, i0) if not (math.isnan(rs[i]) or math.isnan(rb[i]))]
    xs, ys = rb[est], rs[est]
    beta: float
    sigma: float | None = None
    if len(est) >= params.min_sessions and float(np.var(xs)) > 0:
        beta = float(np.cov(ys, xs, ddof=1)[0, 1] / np.var(xs, ddof=1))
        alpha = float(np.mean(ys) - beta * np.mean(xs))
        resid = ys - alpha - beta * xs
        sigma = float(math.sqrt(float(np.sum(resid**2)) / (len(est) - 2)))
    else:
        beta = 1.0
        out["beta_fallback"] = 1
        if len(est) >= params.min_sigma_sessions:
            sigma = float(np.std(ys - xs, ddof=1))
        else:
            out["note"] = f"only {len(est)} sessions before t0: no z-scores"
    out["beta"], out["sigma_resid"] = _r(beta), _r(sigma)

    for k in WINDOWS:
        if i0 + k > last_filled:
            continue
        ar = rs[i0 : i0 + k + 1] - beta * rb[i0 : i0 + k + 1]
        if np.isnan(ar).any():
            continue
        car = float(np.sum(ar))
        out[f"car_{k}"] = _r(car)
        if sigma:
            out[f"z_{k}"] = _r(car / (sigma * math.sqrt(k + 1)))
    out["filled"] = i0 + WINDOWS[-1] <= last_filled

    if i0 >= 1 and i0 + 1 <= last_filled and not math.isnan(ls[i0 - 1]):
        out["ret_raw_1"] = _r(float(ls[i0 + 1] / ls[i0 - 1] - 1.0))

    j = stock.days.index(t0s)
    prior = [v for v in stock.volumes[max(0, j - params.volume_window) : j] if v > 0]
    if len(prior) >= params.volume_window // 2 and median(prior) > 0:
        out["abn_volume"] = _r(stock.volumes[j] / median(prior))

    c1, c20 = out["car_1"], out["car_20"]
    if c1 is not None and c20 is not None and abs(c1) >= params.reversal_min_abs_car1:
        out["reversal_ratio"] = _r(c20 / c1)
    return out


# --------------------------------------------------------------------------- reads


@dataclass(frozen=True)
class EventRef:
    event_id: int
    symbol: str
    published_at: str
    origin: str
    cls: str
    category: str
    materiality: int
    approx_time: bool


def reaction_universe(conn: Connection) -> dict[str, str]:
    """symbol -> benchmark: QTUM for pure-plays, QQQ for the theme ETF."""
    out = {}
    for sym, typ in conn.execute(
        select(tickers.c.symbol, tickers.c.type).where(tickers.c.type.in_(("pure_play", "etf")))
    ):
        out[sym] = "QQQ" if typ == "etf" else "QTUM"
    return out


def load_events(conn: Connection, symbols: list[str]) -> list[EventRef]:
    rows = conn.execute(
        select(
            events.c.id,
            event_tickers.c.symbol,
            events.c.published_at,
            events.c.origin,
            events.c.raw,
            event_classifications.c["class"],
            event_classifications.c.category,
            event_classifications.c.materiality,
        )
        .join(event_tickers, event_tickers.c.event_id == events.c.id)
        .join(event_classifications, event_classifications.c.event_id == events.c.id)
        .where(events.c.quarantined == 0, event_tickers.c.symbol.in_(symbols))
        .order_by(events.c.id, event_tickers.c.symbol)
    ).all()
    out = []
    for r in rows:
        raw = json.loads(r.raw) if r.raw else None
        out.append(
            EventRef(
                r.id,
                r.symbol,
                r.published_at,
                r.origin,
                r._mapping["class"],
                r.category,
                r.materiality,
                is_approx_time(r.origin, raw if isinstance(raw, dict) else None, r.published_at),
            )
        )
    return out


def is_confounder(e: EventRef, params: ReactionParams) -> bool:
    return (
        e.materiality >= params.confound_min_materiality
        or e.category == "earnings_release"
        or (e.cls == "RISK" and e.origin == "edgar")
    )


def confounders_for(
    e: EventRef,
    t0: date,
    by_symbol: dict[str, list[tuple[date, EventRef]]],
) -> list[int]:
    lo = nyse.offset(t0, -1) or t0
    hi = nyse.offset(t0, 5) or t0 + timedelta(days=7)
    return sorted(
        {
            o.event_id
            for ot0, o in by_symbol.get(e.symbol, [])
            if o.event_id != e.event_id and lo <= ot0 <= hi
        }
    )


def compute_rows(conn: Connection, params: ReactionParams) -> list[dict[str, Any]]:
    universe = reaction_universe(conn)
    evs = load_events(conn, sorted(universe))
    anchors: dict[str, date | None] = {}
    for e in evs:
        if e.published_at not in anchors:
            anchors[e.published_at] = anchor(e.published_at)
    by_symbol: dict[str, list[tuple[date, EventRef]]] = defaultdict(list)
    for e in evs:
        t = anchors[e.published_at]
        if t is not None and is_confounder(e, params):
            by_symbol[e.symbol].append((t, e))
    series = load_series(conn, {*universe, *universe.values()})
    rows = []
    for e in evs:
        bench = universe[e.symbol]
        t0 = anchors[e.published_at]
        row: dict[str, Any] = {
            "event_id": e.event_id,
            "symbol": e.symbol,
            "benchmark": bench,
            "approx_time": int(e.approx_time),
            "confounders": "[]",
        }
        if t0 is None:
            rows.append(
                {
                    **row,
                    **dict.fromkeys(VALUE_COLS[2:14]),
                    "t0": None,
                    "beta_fallback": 0,
                    "status": "no_data",
                    "note": "no NYSE session after the event time in the calendar",
                }
            )
            continue
        m = compute(series[e.symbol], series[bench], t0, params)
        conf = confounders_for(e, t0, by_symbol)
        if m["no_data"]:
            status = "no_data"
            m["note"] = m["no_data"]
        elif conf:
            status = "confounded"
        else:
            status = "complete" if m["filled"] else "pending"
        m.pop("filled")
        m.pop("no_data")
        rows.append({**row, **m, "status": status, "confounders": json.dumps(conf)})
    return rows


def _changed(old: dict[str, Any] | None, new: dict[str, Any]) -> bool:
    return old is None or any(old.get(c) != new.get(c) for c in VALUE_COLS)


def run_reactions(engine: Engine, params: ReactionParams) -> JobResult:
    with engine.connect() as conn:
        rows = compute_rows(conn, params)
        existing = {
            (r.event_id, r.symbol): dict(r._mapping) for r in conn.execute(select(event_reactions))
        }
    now = utcnow_iso()
    todo = [
        {**r, "computed_at": now}
        for r in rows
        if _changed(existing.get((r["event_id"], r["symbol"])), r)
    ]
    if todo:
        with write_tx(engine) as conn:
            upsert(conn, event_reactions, todo, key_cols=["event_id", "symbol"])
    return JobResult(rows_written=len(todo))
