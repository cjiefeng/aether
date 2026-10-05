"""FINRA short interest ingest + the `short_interest_spike` rule (spec §4, §5.2 step 1; M8).

1. Network first, no transaction held: for each mid-month and month-end settlement date in the
   last `months_back` months that is at least `min_age_days` old and not fetched yet, try the
   nominal date and up to three weekdays before it (non-business days are found by FINRA's 403).
   Only rows for the strategy universe (QTUM + pure-plays) are kept.
2. One `write_tx`: upsert `short_interest` rows with short % of shares outstanding (the latest
   XBRL cover-page share count on or before the settlement date), record each file in
   `short_interest_files`, then evaluate the spike rule for every pure-play report against the
   report before it and upsert one RISK event per hit (idempotent: the event URL is the file URL
   plus `#SYMBOL`).

Event dates: a file fetched within `LIVE_WINDOW_DAYS` of its settlement date is dated by when
Aether first saw it (about when FINRA published it); older files (the backfill) are dated by the
settlement date, and `raw.dated_by` says which. The alert lookback therefore never fires on the
backfill.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, select

from aether.classify.rules import RuleHit, short_interest_spike
from aether.config import ShortInterestRule
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    event_classifications,
    event_sources,
    event_tickers,
    events,
    fundamentals_q,
    short_interest,
    short_interest_files,
    tickers,
)
from aether.db.types import to_iso
from aether.ingest.edgar import url_hash
from aether.portfolio.holdings import universe_symbols
from aether.providers.finra import (
    HOST,
    FinraShortInterest,
    ShortRow,
    candidates,
    file_url,
    nominal_dates,
)
from aether.providers.prices import ProviderError
from aether.runs import JobResult

log = logging.getLogger(__name__)

LIVE_WINDOW_DAYS = 21
SHARE_CONCEPTS = ("dei:EntityCommonStockSharesOutstanding", "us-gaap:CommonStockSharesOutstanding")
DOMAIN = "finra.org"


def shares_outstanding(conn: Connection, symbol: str, on: str) -> tuple[int, str] | None:
    """The latest cover-page (else balance-sheet) share count dated on or before `on`."""
    for concept in SHARE_CONCEPTS:
        row = conn.execute(
            select(fundamentals_q.c.value_int, fundamentals_q.c.period_end)
            .where(
                fundamentals_q.c.symbol == symbol,
                fundamentals_q.c.concept == concept,
                fundamentals_q.c.value_int.is_not(None),
                fundamentals_q.c.value_int > 0,
                fundamentals_q.c.period_end <= on,
            )
            .order_by(fundamentals_q.c.period_end.desc())
            .limit(1)
        ).first()
        if row is not None:
            return int(row.value_int), str(row.period_end)
    return None


def _pct(short: int, shares: int | None) -> float | None:
    return None if not shares else round(short / shares * 100, 4)


def _fetch_new(
    provider: FinraShortInterest,
    fetched: set[str],
    symbols: set[str],
    rule: ShortInterestRule,
    today: date,
) -> tuple[dict[date, list[ShortRow]], list[str]]:
    files: dict[date, list[ShortRow]] = {}
    errors: list[str] = []
    for nominal in nominal_dates(today, rule.months_back):
        cands = candidates(nominal)
        if nominal + timedelta(days=rule.min_age_days) > today:
            continue
        if any(d.isoformat() in fetched for d in cands):
            continue
        for d in cands:
            try:
                rows = provider.fetch(d, symbols)
            except ProviderError as exc:
                errors.append(f"{d}: {exc}"[:120])
                break
            if rows is not None:
                files[d] = rows
                break
    return files, errors


def write_spike_event(
    conn: Connection,
    symbol: str,
    settlement: str,
    hit: RuleHit,
    published_at: str,
    raw: dict[str, Any],
    now: str,
) -> int:
    url = f"{file_url(date.fromisoformat(settlement))}#{symbol}"
    h = url_hash(url)
    upsert(
        conn,
        events,
        [
            {
                "url_hash": h,
                "title": hit.title[:300],
                "url": url,
                "source_domain": DOMAIN,
                "trust_tier": "T1",
                "independent_source_count": 1,
                "published_at": published_at,
                "excerpt": hit.rationale[:500],
                "origin": "finra",
                "raw": json.dumps(raw, sort_keys=True),
                "created_at": now,
            }
        ],
        key_cols=["url_hash"],
        update_cols=["title", "excerpt", "raw"],
    )
    event_id: int = conn.execute(select(events.c.id).where(events.c.url_hash == h)).scalar_one()
    upsert(
        conn,
        event_sources,
        [
            {
                "event_id": event_id,
                "url": url,
                "domain": DOMAIN,
                "trust_tier": "T1",
                "origin": "finra",
            }
        ],
        key_cols=["event_id", "url"],
    )
    upsert(
        conn,
        event_tickers,
        [{"event_id": event_id, "symbol": symbol, "direction": hit.direction}],
        key_cols=["event_id", "symbol"],
    )
    upsert(
        conn,
        event_classifications,
        [
            {
                "event_id": event_id,
                "class": hit.cls,
                "category": hit.category,
                "materiality_raw": hit.materiality,
                "materiality": hit.materiality,  # T1 regulatory data: no trust-tier cap
                "direction": hit.direction,
                "confidence": hit.confidence,
                "rationale": hit.rationale,
                "evidence_quote": hit.rationale[:500],
                "rule_id": hit.rule_id,
                "model": None,
                "prompt_version": None,
                "created_at": now,
            }
        ],
        key_cols=["event_id"],
    )
    return event_id


def evaluate_spikes(
    conn: Connection,
    rule: ShortInterestRule,
    keys: list[tuple[str, str]],
    first_seen: dict[str, datetime],
    now: datetime,
) -> int:
    """Spike rule for the given (symbol, settlement) pure-play reports; returns events written."""
    pure = set(
        conn.execute(select(tickers.c.symbol).where(tickers.c.type == "pure_play")).scalars()
    )
    n = 0
    for sym, settled in sorted(keys):
        if sym not in pure:
            continue
        cur = conn.execute(
            select(short_interest).where(
                short_interest.c.symbol == sym, short_interest.c.settlement_date == settled
            )
        ).one()
        prev = conn.execute(
            select(short_interest)
            .where(short_interest.c.symbol == sym, short_interest.c.settlement_date < settled)
            .order_by(short_interest.c.settlement_date.desc())
            .limit(1)
        ).first()
        if prev is None:
            continue
        hit = short_interest_spike(
            sym, settled, cur.pct_shares_out, prev.pct_shares_out, prev.settlement_date, rule
        )
        if hit is None:
            continue
        seen = first_seen.get(settled)
        live = (
            seen is not None
            and (seen.date() - date.fromisoformat(settled)).days <= LIVE_WINDOW_DAYS
        )
        published = to_iso(seen) if live and seen is not None else f"{settled}T00:00:00Z"
        raw = {
            "settlement_date": settled,
            "prev_settlement_date": prev.settlement_date,
            "pct_shares_out": cur.pct_shares_out,
            "prev_pct_shares_out": prev.pct_shares_out,
            "short_shares": cur.short_shares,
            "shares_out": cur.shares_out,
            "shares_out_as_of": cur.shares_out_as_of,
            "dated_by": "first_seen" if live else "settlement_date",
        }
        write_spike_event(conn, sym, settled, hit, published, raw, to_iso(now))
        n += 1
    return n


def ingest_short_interest(
    engine: Engine,
    provider: FinraShortInterest,
    rule: ShortInterestRule,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> JobResult:
    now = now or datetime.now(UTC)
    today = today or now.date()
    symbols = set(universe_symbols(engine))
    with engine.connect() as conn:
        fetched = set(conn.execute(select(short_interest_files.c.settlement_date)).scalars())

    files, errors = _fetch_new(provider, fetched, symbols, rule, today)
    if errors and not files:
        raise ProviderError("; ".join(errors)[:500])

    stamp = to_iso(now)
    keys: list[tuple[str, str]] = []
    written = 0
    with write_tx(engine) as conn:
        for d, rows in sorted(files.items()):
            url = file_url(d)
            out = []
            for r in rows:
                so = shares_outstanding(conn, r.symbol, r.settlement_date.isoformat())
                out.append(
                    {
                        "symbol": r.symbol,
                        "settlement_date": r.settlement_date.isoformat(),
                        "short_shares": r.short_shares,
                        "prev_short_shares": r.prev_short_shares,
                        "avg_daily_volume": r.avg_daily_volume,
                        "days_to_cover": r.days_to_cover,
                        "shares_out": so[0] if so else None,
                        "shares_out_as_of": so[1] if so else None,
                        "pct_shares_out": _pct(r.short_shares, so[0] if so else None),
                        "source": "finra",
                        "source_url": url,
                        "fetched_at": stamp,
                    }
                )
                keys.append((r.symbol, r.settlement_date.isoformat()))
            written += upsert(conn, short_interest, out, key_cols=["symbol", "settlement_date"])
            upsert(
                conn,
                short_interest_files,
                [
                    {
                        "settlement_date": d.isoformat(),
                        "url": url,
                        "rows": len(out),
                        "fetched_at": stamp,
                    }
                ],
                key_cols=["settlement_date"],
            )
        first_seen = {settled: now for _sym, settled in keys}
        written += evaluate_spikes(conn, rule, keys, first_seen, now)
    return JobResult(
        rows_written=written,
        provider=HOST,
        warning="; ".join(errors)[:500] if errors else None,
    )
