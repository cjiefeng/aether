"""My sleeve vs QTUM vs QQQ on `/holdings` (issue #24). Pure functions; the loader at the bottom
reads only (the dashboard's read-only engine). No LLM; nothing leaves the machine.

All three lines are total return (dividends reinvested), rebased to 100 at the range start.

- **Actual:** a time-weighted return of the sleeve as it was held. A `holdings_history` snapshot
  applied at T counts as held from the close of the last NYSE session that ended before T (so a
  Saturday entry earns Monday's return). The return on day t is
  `sum_i v_i,p * R_i,t / V_p` over the holdings in effect at the previous included day p, where
  `v` is shares x close, `V` adds sleeve cash (which earns 0%) and `R` is the symbol's total
  return from p to t. Holdings changes reweight; they never create return. A zero-value sleeve
  adds 0%.
- **Current (hypothetical):** today's share counts and cash, held unchanged over the range.

A held symbol's (or a benchmark's) missing close is carried forward up to `MAX_LAG_DAYS`
calendar days; past that the day is skipped for every line and `stale_since` is set.

The output carries index levels, percentages, dates and snapshot sources only: never share
counts, values, cost basis or account numbers.
"""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any, Literal

import numpy as np
from sqlalchemy import Engine, select

from aether.db.models import dividends, holdings_history, prices_daily
from aether.market import HOLDINGS_MAX_LAG_DAYS, RANGES
from aether.portfolio.holdings import read_holdings
from aether.portfolio.metrics import compute_metrics
from aether.portfolio.total_return import align, total_return_levels

CORE = "QTUM"
MARKET = "QQQ"
SINCE = "since"  # range key: from the first holdings record
RANGE_KEYS = (*RANGES, SINCE)
MAX_LAG_DAYS = HOLDINGS_MAX_LAG_DAYS
SLEEVE = "My sleeve"
SLEEVE_HYPOTHETICAL = "My sleeve (hypothetical: current holdings)"
# Annualized return only when the lines span about a year (a 1y range's first session can fall a
# few days after its start date). Keyed on the data shown, not the range button: a 1y range over
# 7 months of tracking isn't annualized.
ANNUALIZE_FROM_DAYS = 358
Mode = Literal["actual", "current"]
MODES: tuple[Mode, ...] = ("actual", "current")


@dataclass(frozen=True)
class Snapshot:
    applied_at: datetime  # UTC
    source: str  # manual | tiger | import
    positions: Mapping[str, float]  # symbol -> shares (> 0)
    cash: float


@dataclass(frozen=True)
class PerfInputs:
    closes: Mapping[str, Sequence[tuple[str, float]]]  # symbol -> [(YYYY-MM-DD, close)] asc
    dividends: Mapping[str, Mapping[str, float]]  # symbol -> ex_date -> cash per share
    snapshots: Sequence[Snapshot]  # holdings_history `after`, ascending applied_at
    current: Snapshot | None  # today's holdings (hypothetical mode)
    # NYSE sessions with their UTC closes, ascending, spanning the price history.
    session_closes: Sequence[tuple[str, datetime]] = field(default_factory=list)


# --------------------------------------------------------------------------- timing


def anchor_session(applied_at: datetime, session_closes: Sequence[tuple[str, datetime]]) -> str:
    """The session whose close a snapshot applied at `applied_at` is held from: the last session
    that closed strictly before it ("" if that's before the calendar)."""
    closes = [c for _, c in session_closes]
    i = bisect.bisect_left(closes, applied_at)
    return session_closes[i - 1][0] if i > 0 else ""


def _same(a: Snapshot, b: Snapshot) -> bool:
    return dict(a.positions) == dict(b.positions) and a.cash == b.cash


def effective_snapshots(
    snapshots: Sequence[Snapshot], session_closes: Sequence[tuple[str, datetime]]
) -> list[tuple[str, Snapshot]]:
    """(anchor session, snapshot) for each change; unchanged re-syncs are dropped, and of several
    changes anchored to the same session the last one wins."""
    out: list[tuple[str, Snapshot]] = []
    for s in snapshots:
        if out and _same(out[-1][1], s):
            continue
        a = anchor_session(s.applied_at, session_closes)
        if out and out[-1][0] == a:
            out[-1] = (a, s)
            if len(out) > 1 and _same(out[-2][1], s):
                out.pop()  # a same-session edit undone
        else:
            out.append((a, s))
    return out


# --------------------------------------------------------------------------- compute


def _range_start(range_key: str, today: date, tracking: str | None, first_day: str) -> str:
    if range_key == SINCE:
        return tracking or first_day
    return (today - timedelta(days=RANGES[range_key])).isoformat()


def _last_close_on_or_before(days: Sequence[str], d: str) -> str | None:
    i = bisect.bisect_right(days, d)
    return days[i - 1] if i else None


def _lag_days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def compute_performance(
    inp: PerfInputs, range_key: str, mode: Mode, today: date, ann: int = 252
) -> dict[str, Any]:
    """The `/api/holdings/performance` body (before rounding)."""
    calendar = sorted({d for s in (CORE, MARKET) for d, _ in inp.closes.get(s, [])})
    changes = effective_snapshots(inp.snapshots, inp.session_closes)
    tracking = changes[0][0] or (calendar[0] if calendar else None) if changes else None
    out: dict[str, Any] = {
        "range": range_key,
        "mode": mode,
        "start": None,
        "end": None,
        "tracking_started": tracking,
        "series": [],
        "markers": [],
        "stats": None,
        "stale_since": None,
        "enough": False,
    }
    if not calendar:
        return out
    if mode == "current":
        if inp.current is None:
            return out
        changes = [("", inp.current)]
    elif not changes:
        return out

    start = _range_start(range_key, today, tracking, calendar[0])
    if mode == "actual" and tracking:
        start = max(start, tracking)  # the Actual line begins when tracking does
    symbols = sorted({CORE, MARKET} | {s for _, snap in changes for s in snap.positions})
    own_days = {s: [d for d, _ in inp.closes.get(s, [])] for s in symbols}
    price = {s: align(list(inp.closes.get(s, [])), calendar) for s in symbols}
    level = {
        s: align(
            total_return_levels(list(inp.closes.get(s, [])), dict(inp.dividends.get(s, {}))),
            calendar,
        )
        for s in symbols
    }
    anchors = [a for a, _ in changes]

    def held_at(day: str) -> Snapshot | None:
        i = bisect.bisect_right(anchors, day) - 1
        return changes[i][1] if i >= 0 else None

    def stale(i: int, prev: int | None, held: Snapshot | None) -> str | None:
        """Why calendar[i] can't be used: the last close date of the first symbol too old to
        carry to it (or the day itself if a needed symbol has no close by then or by `prev`)."""
        d = calendar[i]
        need = [CORE, MARKET, *(held.positions if held else ())]
        for s in need:
            last = _last_close_on_or_before(own_days[s], d)
            if last is None or (prev is not None and math.isnan(float(level[s][prev]))):
                return d
            if _lag_days(last, d) > MAX_LAG_DAYS:
                return last
        return None

    first = bisect.bisect_left(calendar, start)
    included: list[int] = []
    stale_since: str | None = None
    for i in range(first, len(calendar)):
        prev = included[-1] if included else None
        held = held_at(calendar[prev] if prev is not None else calendar[i])
        s = stale(i, prev, held)
        if s is not None:
            stale_since = s if stale_since is None else min(stale_since, s)
            continue
        included.append(i)
    if _lag_days(calendar[-1], today.isoformat()) > MAX_LAG_DAYS:
        stale_since = calendar[-1] if stale_since is None else min(stale_since, calendar[-1])
    out["stale_since"] = stale_since
    if not included:
        return out

    dates = [calendar[i] for i in included]
    r_sleeve, r_core, r_mkt = [], [], []
    for p, t in pairwise(included):
        held = held_at(calendar[p])
        num, value = 0.0, 0.0
        if held is not None:
            value = held.cash
            for sym, sh in held.positions.items():
                v = sh * float(price[sym][p])
                value += v
                num += v * (float(level[sym][t]) / float(level[sym][p]) - 1.0)
        r_sleeve.append(num / value if value > 0 else 0.0)
        r_core.append(float(level[CORE][t] / level[CORE][p]) - 1.0)
        r_mkt.append(float(level[MARKET][t] / level[MARKET][p]) - 1.0)

    def index(r: list[float]) -> list[list[Any]]:
        lv = 100.0 * np.concatenate(([1.0], np.cumprod(1.0 + np.asarray(r, dtype=float))))
        return [[d, float(x)] for d, x in zip(dates, lv, strict=True)]

    name = SLEEVE if mode == "actual" else SLEEVE_HYPOTHETICAL
    out["series"] = [
        {"name": name, "data": index(r_sleeve)},
        {"name": CORE, "data": index(r_core)},
        {"name": MARKET, "data": index(r_mkt)},
    ]
    out["start"], out["end"] = dates[0], dates[-1]
    if mode == "actual":
        shown = set(dates)
        out["markers"] = [{"date": a, "source": s.source} for a, s in changes if a in shown]
    out["enough"] = len(dates) >= 2
    if len(r_sleeve) >= 2:
        out["stats"] = _stats({name: r_sleeve, CORE: r_core, MARKET: r_mkt}, name, dates, ann)
    return out


def _stats(
    returns: Mapping[str, list[float]], sleeve: str, days: list[str], ann: int
) -> dict[str, Any]:
    """Per line: total return, annualized return (about a year or more of data), volatility, max
    drawdown and beta vs QQQ, as `compute_metrics` defines them; plus sleeve excess returns in
    percentage points. `days` are the index dates (the base, then one per return)."""
    annualize = _lag_days(days[0], days[-1]) >= ANNUALIZE_FROM_DAYS
    dates = days[1:]
    mkt = np.asarray(returns[MARKET], dtype=float)
    lines: dict[str, dict[str, float | None]] = {}
    for name, r in returns.items():
        m = compute_metrics(np.asarray(r, dtype=float), dates, {MARKET: mkt}, ann=ann)
        lines[name] = {
            "total_return": _num(m["total_return"]),
            "annualized_return": _num(m["cagr"]) if annualize else None,
            "volatility": _num(m["volatility"]),
            "max_drawdown": _num(m["max_drawdown"]),
            "beta_qqq": None if name == MARKET else _num(m[f"beta_{MARKET}"]),
        }
    s = lines[sleeve]["total_return"]

    def pp(other: str) -> float | None:
        o = lines[other]["total_return"]
        return None if s is None or o is None else (s - o) * 100.0

    return {
        "annualized": annualize,
        "lines": [{"name": n, **v} for n, v in lines.items()],
        "excess_pp": {CORE: pp(CORE), MARKET: pp(MARKET)},
    }


def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, int | float) and math.isfinite(float(x)) else None


def rounded(body: dict[str, Any], dp: int = 6) -> dict[str, Any]:
    """Round floats for the JSON response."""

    def r(x: Any) -> Any:
        if isinstance(x, float):
            return round(x, dp)
        if isinstance(x, dict):
            return {k: r(v) for k, v in x.items()}
        if isinstance(x, list):
            return [r(v) for v in x]
        return x

    out: dict[str, Any] = r(body)
    return out


# --------------------------------------------------------------------------- loader (read-only)


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _snapshot(raw: str, applied_at: datetime, source: str) -> Snapshot:
    body = json.loads(raw)
    positions = {
        sym: float(p["shares"])
        for sym, p in body.get("positions", {}).items()
        if float(p["shares"]) > 0
    }
    return Snapshot(applied_at, source, positions, float(body.get("cash", "0")))


def has_history(engine: Engine) -> bool:
    with engine.connect() as conn:
        return conn.execute(select(holdings_history.c.id).limit(1)).first() is not None


def load_inputs(engine: Engine, today: date) -> PerfInputs:
    from aether import nyse

    with engine.connect() as conn:
        snaps = [
            _snapshot(r.after, _parse_ts(r.applied_at), r.source)
            for r in conn.execute(
                select(
                    holdings_history.c.after,
                    holdings_history.c.applied_at,
                    holdings_history.c.source,
                ).order_by(holdings_history.c.applied_at, holdings_history.c.id)
            )
        ]
        h = read_holdings(conn)
        current = Snapshot(
            datetime.now(UTC),
            "manual",
            {s: float(p.shares) for s, p in h.positions.items()},
            float(h.cash),
        )
        symbols = sorted(
            {CORE, MARKET, *current.positions} | {s for sn in snaps for s in sn.positions}
        )
        closes: dict[str, list[tuple[str, float]]] = {s: [] for s in symbols}
        for sym, d, c in conn.execute(
            select(prices_daily.c.symbol, prices_daily.c.d, prices_daily.c.c)
            .where(prices_daily.c.symbol.in_(symbols))
            .order_by(prices_daily.c.symbol, prices_daily.c.d)
        ):
            closes[sym].append((d, c))
        divs: dict[str, dict[str, float]] = {}
        for sym, ex, amt in conn.execute(
            select(dividends.c.symbol, dividends.c.ex_date, dividends.c.amount_micros).where(
                dividends.c.symbol.in_(symbols)
            )
        ):
            divs.setdefault(sym, {})[ex] = float(amt)
    first = min((rows[0][0] for rows in closes.values() if rows), default=today.isoformat())
    sess = nyse.sessions(
        max(nyse.START, date.fromisoformat(first) - timedelta(days=10)),
        today + timedelta(days=10),
    )
    session_closes = [(d.isoformat(), nyse.close_utc(d)) for d in sess]
    return PerfInputs(closes, divs, snaps, None if h.empty else current, session_closes)


def performance(
    engine: Engine, range_key: str, mode: Mode, today: date, ann: int = 252
) -> dict[str, Any]:
    return rounded(compute_performance(load_inputs(engine, today), range_key, mode, today, ann))
