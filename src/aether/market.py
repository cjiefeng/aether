"""Read-side market computations for the dashboard: pure functions plus read-only queries.

Nothing here writes. Callers pass the dashboard's `mode=ro` engine.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

from sqlalchemy import Engine, func, select

from aether.db.models import job_runs, prices_daily, qtum_holdings, tickers

Series = list[tuple[str, float]]  # [(YYYY-MM-DD, value)], ascending

RANGES: dict[str, int] = {"1m": 30, "3m": 91, "6m": 182, "1y": 365, "2y": 730}
DEFAULT_RANGE = "1y"
BASKET = "Pure-play basket"

# Freshness. A stand-in for a holiday calendar until M7 brings exchange_calendars.
PRICES_JOB_MAX_AGE = timedelta(hours=30)
SYMBOL_MAX_LAG_DAYS = 4
HOLDINGS_MAX_LAG_DAYS = 4


# --------------------------------------------------------------------------- pure functions


def rebase(series: Series, base: float = 100.0) -> Series:
    if not series:
        return []
    first = series[0][1]
    return [(d, round(v / first * base, 4)) for d, v in series]


def window(series: Series, days: int, end: date) -> Series:
    start = (end - timedelta(days=days)).isoformat()
    return [(d, v) for d, v in series if d >= start]


def basket_series(members: Mapping[str, Series]) -> Series:
    """Equal-weighted basket level (starts at 100), rebalanced daily.

    A name joins once it has bars: from its second bar on, its daily return is included in that
    day's mean. Days on which no member has a return carry the previous level.
    """
    returns: dict[str, list[float]] = {}
    all_days: set[str] = set()
    for s in members.values():
        all_days.update(d for d, _ in s)
        for (_, prev), (d, cur) in pairwise(s):
            returns.setdefault(d, []).append(cur / prev - 1.0)
    out: Series = []
    level = 100.0
    for d in sorted(all_days):
        rs = returns.get(d)
        if rs:
            level *= 1.0 + sum(rs) / len(rs)
        out.append((d, round(level, 4)))
    return out


@dataclass(frozen=True)
class TickerSummary:
    symbol: str
    type: str
    last_d: str | None = None
    last_c: float | None = None
    chg_1d: float | None = None  # fraction, e.g. 0.012 = +1.2%
    chg_30d: float | None = None
    drawdown_52w: float | None = None  # <= 0
    provider: str | None = None
    stale: bool = True


def summarize(
    symbol: str, type_: str, series: Series, provider: str | None, today: date
) -> TickerSummary:
    if not series:
        return TickerSummary(symbol, type_)
    last_d, last_c = series[-1]
    chg_1d = last_c / series[-2][1] - 1 if len(series) >= 2 else None
    ref_30 = (date.fromisoformat(last_d) - timedelta(days=30)).isoformat()
    before = [v for d, v in series if d <= ref_30]
    chg_30d = last_c / before[-1] - 1 if before else None
    yr = (date.fromisoformat(last_d) - timedelta(days=365)).isoformat()
    high = max(v for d, v in series if d >= yr)
    return TickerSummary(
        symbol=symbol,
        type=type_,
        last_d=last_d,
        last_c=last_c,
        chg_1d=chg_1d,
        chg_30d=chg_30d,
        drawdown_52w=last_c / high - 1,
        provider=provider,
        stale=symbol_stale(last_d, today),
    )


def symbol_stale(last_d: str | None, today: date) -> bool:
    if last_d is None:
        return True
    return (today - date.fromisoformat(last_d)).days > SYMBOL_MAX_LAG_DAYS


def job_stale(last_ok: str | None, now: datetime, max_age: timedelta) -> bool:
    if last_ok is None:
        return True
    return now - datetime.fromisoformat(last_ok) > max_age


# --------------------------------------------------------------------------- read queries


def load_tickers(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(tickers.c.symbol, tickers.c.type)
            .where(tickers.c.active == 1)
            .order_by(tickers.c.symbol)
        ).all()
    return [(s, t) for s, t in rows]


def load_closes(engine: Engine, symbols: Iterable[str]) -> dict[str, Series]:
    syms = list(symbols)
    out: dict[str, Series] = {s: [] for s in syms}
    with engine.connect() as conn:
        rows = conn.execute(
            select(prices_daily.c.symbol, prices_daily.c.d, prices_daily.c.c)
            .where(prices_daily.c.symbol.in_(syms))
            .order_by(prices_daily.c.symbol, prices_daily.c.d)
        ).all()
    for sym, d, c in rows:
        out[sym].append((d, c))
    return out


def load_ohlcv(engine: Engine, symbol: str) -> list[dict[str, object]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(prices_daily).where(prices_daily.c.symbol == symbol).order_by(prices_daily.c.d)
        ).all()
    return [
        {"d": r.d, "o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.volume, "p": r.provider}
        for r in rows
    ]


def latest_providers(engine: Engine) -> dict[str, str]:
    """Provider that served each symbol's most recent bar."""
    latest = (
        select(prices_daily.c.symbol, func.max(prices_daily.c.d).label("d"))
        .group_by(prices_daily.c.symbol)
        .subquery()
    )
    with engine.connect() as conn:
        rows = conn.execute(
            select(prices_daily.c.symbol, prices_daily.c.provider).join(
                latest,
                (prices_daily.c.symbol == latest.c.symbol) & (prices_daily.c.d == latest.c.d),
            )
        ).all()
    return {s: p for s, p in rows}


def last_ok_finished(engine: Engine, job: str) -> str | None:
    with engine.connect() as conn:
        ts: str | None = conn.execute(
            select(func.max(job_runs.c.finished_at)).where(
                job_runs.c.job == job, job_runs.c.status == "ok"
            )
        ).scalar()
    return ts


@dataclass(frozen=True)
class HoldingsView:
    snapshot_date: str | None
    weights: dict[str, float]  # watchlist symbol -> % of QTUM
    combined: float | None
    stale: bool


def holdings_view(engine: Engine, watch: Sequence[str], today: date) -> HoldingsView:
    with engine.connect() as conn:
        snap: str | None = conn.execute(select(func.max(qtum_holdings.c.snapshot_date))).scalar()
        if snap is None:
            return HoldingsView(None, {}, None, True)
        rows = conn.execute(
            select(qtum_holdings.c.holding_symbol, qtum_holdings.c.weight).where(
                qtum_holdings.c.snapshot_date == snap,
                qtum_holdings.c.holding_symbol.in_(list(watch)),
            )
        ).all()
    weights = {s: w for s, w in rows}
    # The issuer may date a snapshot ahead (next session), so only lateness counts as stale.
    stale = (today - date.fromisoformat(snap)).days > HOLDINGS_MAX_LAG_DAYS
    return HoldingsView(snap, weights, round(sum(weights.values()), 2), stale)


@dataclass(frozen=True)
class Freshness:
    prices_last_ok: str | None
    prices_stale: bool
    stale_symbols: tuple[tuple[str, str | None], ...]  # (symbol, last bar date)


def freshness(
    engine: Engine, summaries: Sequence[TickerSummary], now: datetime | None = None
) -> Freshness:
    now = now or datetime.now(UTC)
    last_ok = last_ok_finished(engine, "prices")
    return Freshness(
        prices_last_ok=last_ok,
        prices_stale=job_stale(last_ok, now, PRICES_JOB_MAX_AGE),
        stale_symbols=tuple((s.symbol, s.last_d) for s in summaries if s.stale),
    )


def overview_series(engine: Engine, range_key: str, today: date) -> dict[str, Series]:
    """QTUM vs pure-play basket vs SOXX vs QQQ, each rebased to 100 at the window start."""
    days = RANGES[range_key]
    types = dict(load_tickers(engine))
    pure = [s for s, t in types.items() if t == "pure_play"]
    lines = [s for s in ("QTUM", "SOXX", "QQQ") if s in types]
    closes = load_closes(engine, [*lines, *pure])
    out: dict[str, Series] = {}
    for sym in lines:
        out[sym] = rebase(window(closes[sym], days, today))
    basket = basket_series({s: closes[s] for s in pure if closes[s]})
    out[BASKET] = rebase(window(basket, days, today))
    return out
