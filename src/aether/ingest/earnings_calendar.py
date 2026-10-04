"""Earnings calendar (spec §4): past dates from 8-K Item 2.02 filings, upcoming from yfinance.

Past rows (`reported`) come from the `filings` table, so this job reads what the edgar job wrote.
Upcoming rows (`scheduled`) are replaced per symbol on each run, because companies move dates.
A scheduled date that has passed without an 8-K 2.02 is kept as-is (the page shows it).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, delete, select

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import earnings_calendar, filings, tickers
from aether.db.types import utcnow_iso
from aether.providers.prices import US_EASTERN, ProviderError
from aether.runs import JobResult

log = logging.getLogger(__name__)

UpcomingFn = Callable[[str], list[date]]


def _yf_upcoming(symbol: str) -> list[date]:
    import yfinance as yf

    yf.set_tz_cache_location("/tmp/yfinance")  # noqa: S108 (read-only root FS; /tmp is tmpfs)
    cal: Any = yf.Ticker(symbol).calendar
    return upcoming_from_calendar(cal)


def upcoming_from_calendar(cal: Any) -> list[date]:
    """yfinance `Ticker.calendar` is a dict with an "Earnings Date" list of dates (or nothing)."""
    if not isinstance(cal, dict):
        return []
    raw = cal.get("Earnings Date") or []
    out: list[date] = []
    for v in raw if isinstance(raw, list | tuple) else [raw]:
        if isinstance(v, datetime):
            out.append(v.date())
        elif isinstance(v, date):
            out.append(v)
    return sorted(set(out))


def reported_from_filings(engine: Engine, symbols: Sequence[str]) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(filings.c.symbol, filings.c.filed_at, filings.c["items"], filings.c.url).where(
                filings.c.symbol.in_(list(symbols)), filings.c.form.in_(["8-K", "8-K/A"])
            )
        ).all()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for sym, filed_at, items, url in rows:
        if "2.02" in json.loads(items):
            # An 8-K/A re-files the same release; the earliest filing date wins.
            key = (sym, filed_at)
            out.setdefault(key, {"symbol": sym, "date": filed_at, "source_url": url})
    return sorted(out.values(), key=lambda r: (r["symbol"], r["date"]))


def ingest_earnings_calendar(
    engine: Engine,
    upcoming: UpcomingFn = _yf_upcoming,
    *,
    today: date | None = None,
) -> JobResult:
    today = today or datetime.now(US_EASTERN).date()
    with engine.connect() as conn:
        symbols = list(
            conn.execute(
                select(tickers.c.symbol).where(tickers.c.active == 1, tickers.c.type == "pure_play")
            ).scalars()
        )

    # Network first.
    scheduled: dict[str, list[date]] = {}
    errors: list[str] = []
    for sym in symbols:
        try:
            scheduled[sym] = [d for d in upcoming(sym) if d >= today]
        except Exception as exc:  # yfinance raises many exception types
            log.warning("earnings calendar %s failed: %s", sym, exc)
            errors.append(f"{sym}: {type(exc).__name__}")
    if symbols and len(errors) == len(symbols):
        raise ProviderError("all symbols failed: " + " | ".join(errors))

    now = utcnow_iso()
    reported = [
        {**r, "status": "reported", "source": "8k_2.02", "fetched_at": now}
        for r in reported_from_filings(engine, symbols)
    ]
    reported_keys = {(r["symbol"], r["date"]) for r in reported}
    sched_rows = [
        {
            "symbol": sym,
            "date": d.isoformat(),
            "status": "scheduled",
            "source": "yfinance",
            "source_url": None,
            "fetched_at": now,
        }
        for sym, ds in scheduled.items()
        for d in ds
        if (sym, d.isoformat()) not in reported_keys
    ]
    with write_tx(engine) as conn:
        for sym in scheduled:
            conn.execute(
                delete(earnings_calendar).where(
                    earnings_calendar.c.symbol == sym,
                    earnings_calendar.c.status == "scheduled",
                    earnings_calendar.c.date >= today.isoformat(),
                )
            )
        n = upsert(conn, earnings_calendar, reported, key_cols=["symbol", "date"])
        n += upsert(conn, earnings_calendar, sched_rows, key_cols=["symbol", "date"])
    return JobResult(
        rows_written=n,
        provider="sec+yfinance",
        warning=("; ".join(errors))[:500] if errors else None,
    )
