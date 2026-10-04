"""Daily prices for every active ticker (watchlist, benchmarks, context).

- First run per symbol backfills `BACKFILL_DAYS` (2 years, the Massive free-tier limit).
- Later runs re-fetch from `last date - REVISION_OVERLAP_DAYS`, so late corrections land.
- A full 2-year refresh (`full_refresh=True`, Sundays) catches retroactive split adjustments.
- All network I/O happens first; then one `write_tx` upserts on PK(symbol, d). Re-runs are
  idempotent.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import Engine, func, select

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import prices_daily, tickers
from aether.db.types import utcnow_iso
from aether.providers.prices import US_EASTERN, Bar, PriceProvider, ProviderError
from aether.runs import JobResult

log = logging.getLogger(__name__)

BACKFILL_DAYS = 730
REVISION_OVERLAP_DAYS = 10


def active_symbols(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(tickers.c.symbol).where(tickers.c.active == 1).order_by(tickers.c.symbol)
            ).scalars()
        )


def last_bar_dates(engine: Engine) -> dict[str, date]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(prices_daily.c.symbol, func.max(prices_daily.c.d)).group_by(
                prices_daily.c.symbol
            )
        ).all()
    return {sym: date.fromisoformat(d) for sym, d in rows}


def plan_ranges(
    symbols: Iterable[str], last: dict[str, date], today: date, *, full_refresh: bool
) -> dict[str, tuple[date, date]]:
    """Per-symbol inclusive fetch window."""
    floor = today - timedelta(days=BACKFILL_DAYS)
    out: dict[str, tuple[date, date]] = {}
    for sym in symbols:
        prev = last.get(sym)
        if full_refresh or prev is None:
            start = floor
        else:
            start = max(floor, prev - timedelta(days=REVISION_OVERLAP_DAYS))
        out[sym] = (start, today)
    return out


def valid_bar(bar: Bar, today: date) -> bool:
    nums = (bar.o, bar.h, bar.l, bar.c)
    return (
        all(math.isfinite(v) and v > 0 for v in nums)
        and bar.h >= bar.l
        and bar.volume >= 0
        and bar.d <= today
    )


def ingest_prices(
    engine: Engine,
    provider: PriceProvider,
    *,
    symbols: Sequence[str] | None = None,
    today: date | None = None,
    full_refresh: bool = False,
) -> JobResult:
    today = today or datetime.now(US_EASTERN).date()
    symbols = list(symbols) if symbols is not None else active_symbols(engine)
    ranges = plan_ranges(symbols, last_bar_dates(engine), today, full_refresh=full_refresh)

    # 1) Network, outside any transaction.
    fetched_at = utcnow_iso()
    rows: list[dict[str, object]] = []
    providers_used: set[str] = set()
    errors: list[str] = []
    dropped = 0
    for sym, (start, end) in ranges.items():
        try:
            bars = provider.fetch_daily(sym, start, end)
        except ProviderError as exc:
            log.warning("prices %s failed: %s", sym, exc)
            errors.append(f"{sym}: {exc}")
            continue
        if not bars:
            log.info("prices %s: no bars for %s..%s", sym, start, end)
        for b in bars:
            if not valid_bar(b, today):
                dropped += 1
                continue
            providers_used.add(b.provider)
            rows.append(
                {
                    "symbol": sym,
                    "d": b.d.isoformat(),
                    "o": b.o,
                    "h": b.h,
                    "l": b.l,
                    "c": b.c,
                    "volume": b.volume,
                    "provider": b.provider,
                    "fetched_at": fetched_at,
                }
            )
    if dropped:
        log.warning("prices: dropped %d invalid bars", dropped)
    if symbols and len(errors) == len(symbols):
        raise ProviderError("all symbols failed: " + " | ".join(errors))

    # 2) One short write transaction.
    with write_tx(engine) as conn:
        upsert(conn, prices_daily, rows, key_cols=["symbol", "d"])

    return JobResult(
        rows_written=len(rows),
        provider="+".join(sorted(providers_used)) or None,
        warning=("; ".join(errors))[:500] if errors else None,
    )
