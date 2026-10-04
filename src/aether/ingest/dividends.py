"""Cash dividends for the backtest universe and benchmarks (QTUM, pure-plays, QQQ, SOXX).

Network first (`BACKFILL_DAYS`, matching the price backfill and the Massive free-tier depth),
then one `write_tx` upserting on PK(symbol, ex_date). Re-runs are idempotent.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import Engine, select

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import dividends, tickers
from aether.db.types import utcnow_iso
from aether.ingest.prices import BACKFILL_DAYS
from aether.providers.dividends import DividendProvider
from aether.providers.prices import US_EASTERN, ProviderError
from aether.runs import JobResult

log = logging.getLogger(__name__)

DIVIDEND_TYPES = ("etf", "pure_play", "benchmark")


def dividend_symbols(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(tickers.c.symbol)
                .where(tickers.c.active == 1, tickers.c.type.in_(DIVIDEND_TYPES))
                .order_by(tickers.c.symbol)
            ).scalars()
        )


def ingest_dividends(
    engine: Engine,
    provider: DividendProvider,
    *,
    symbols: Sequence[str] | None = None,
    today: date | None = None,
) -> JobResult:
    today = today or datetime.now(US_EASTERN).date()
    symbols = list(symbols) if symbols is not None else dividend_symbols(engine)
    start = today - timedelta(days=BACKFILL_DAYS)

    fetched_at = utcnow_iso()
    rows: list[dict[str, object]] = []
    providers_used: set[str] = set()
    errors: list[str] = []
    for sym in symbols:
        try:
            divs = provider.fetch_dividends(sym, start, today)
        except ProviderError as exc:
            log.warning("dividends %s failed: %s", sym, exc)
            errors.append(f"{sym}: {exc}")
            continue
        for d in divs:
            providers_used.add(d.provider)
            rows.append(
                {
                    "symbol": sym,
                    "ex_date": d.ex_date.isoformat(),
                    "amount_micros": d.amount,
                    "currency": "USD",
                    "provider": d.provider,
                    "fetched_at": fetched_at,
                }
            )
    if symbols and len(errors) == len(symbols):
        raise ProviderError("all symbols failed: " + " | ".join(errors))

    with write_tx(engine) as conn:
        upsert(conn, dividends, rows, key_cols=["symbol", "ex_date"])

    return JobResult(
        rows_written=len(rows),
        provider="+".join(sorted(providers_used)) or None,
        warning=("; ".join(errors))[:500] if errors else None,
    )
