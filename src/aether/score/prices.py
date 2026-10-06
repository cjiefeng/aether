"""Total-return price series for the M9 scores (reactions, theme, scorecard). Reads only.

Levels are total-return (split-adjusted closes + cash dividends, `portfolio/total_return.py`), so a
dividend's ex-date drop isn't read as a move.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Connection, select

from aether.db.models import dividends, prices_daily
from aether.portfolio.total_return import total_return_levels


@dataclass(frozen=True)
class PriceSeries:
    symbol: str
    days: tuple[str, ...]  # actual bar dates, ascending
    closes: tuple[float, ...]
    levels: tuple[float, ...]  # total-return level per bar
    volumes: tuple[int, ...]

    @property
    def first(self) -> str | None:
        return self.days[0] if self.days else None

    @property
    def last(self) -> str | None:
        return self.days[-1] if self.days else None


def load_series(conn: Connection, symbols: Iterable[str]) -> dict[str, PriceSeries]:
    out: dict[str, PriceSeries] = {}
    for s in sorted(set(symbols)):
        bars = conn.execute(
            select(prices_daily.c.d, prices_daily.c.c, prices_daily.c.volume)
            .where(prices_daily.c.symbol == s)
            .order_by(prices_daily.c.d)
        ).all()
        divs = {
            ex: float(a)
            for ex, a in conn.execute(
                select(dividends.c.ex_date, dividends.c.amount_micros).where(
                    dividends.c.symbol == s
                )
            )
        }
        closes = [(b.d, float(b.c)) for b in bars]
        levels = total_return_levels(closes, divs)
        out[s] = PriceSeries(
            s,
            tuple(d for d, _ in closes),
            tuple(c for _, c in closes),
            tuple(v for _, v in levels),
            tuple(int(b.volume or 0) for b in bars),
        )
    return out


def upto(s: PriceSeries, as_of: date) -> PriceSeries:
    """The series cut at `as_of` (inclusive)."""
    n = bisect.bisect_right(s.days, as_of.isoformat())
    return PriceSeries(s.symbol, s.days[:n], s.closes[:n], s.levels[:n], s.volumes[:n])
