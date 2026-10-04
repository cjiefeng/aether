"""Read-side SEC views for the dashboard (filings, insiders, capital structure, lock-ups, earnings,
risk events). Nothing here writes; callers pass the `mode=ro` engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Engine, select

from aether.db.models import (
    capital_structure,
    earnings_calendar,
    event_classifications,
    event_tickers,
    events,
    filings,
    fundamentals_q,
    insider_txns,
    lockups,
)
from aether.market import job_stale, last_ok_finished

EDGAR_JOB_MAX_AGE = timedelta(hours=36)
EARNINGS_JOB_MAX_AGE = timedelta(hours=36)
DILUTION_CONCEPTS = (
    "dei:EntityCommonStockSharesOutstanding",
    "us-gaap:CommonStockSharesOutstanding",
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
)
CONCEPT_LABELS = {
    "dei:EntityCommonStockSharesOutstanding": "Shares outstanding (cover page)",
    "us-gaap:CommonStockSharesOutstanding": "Common shares outstanding (balance sheet)",
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding": "Weighted avg. diluted shares (qtr)",
}


@dataclass(frozen=True)
class FilingRow:
    filed_at: str
    form: str
    items: tuple[str, ...]
    description: str | None
    url: str
    cls: str | None
    category: str | None
    materiality: int | None


def filings_for(engine: Engine, symbol: str, limit: int = 60) -> list[FilingRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                filings.c.filed_at,
                filings.c.form,
                filings.c["items"],
                filings.c.primary_doc_description,
                filings.c.url,
                event_classifications.c["class"],
                event_classifications.c.category,
                event_classifications.c.materiality,
            )
            .select_from(
                filings.outerjoin(events, events.c.accession == filings.c.accession).outerjoin(
                    event_classifications, event_classifications.c.event_id == events.c.id
                )
            )
            .where(filings.c.symbol == symbol)
            .order_by(filings.c.filed_at.desc(), filings.c.accession.desc())
            .limit(limit)
        ).all()
    return [
        FilingRow(d, form, tuple(json.loads(items)), desc, url, cls, cat, mat)
        for d, form, items, desc, url, cls, cat, mat in rows
    ]


@dataclass(frozen=True)
class InsiderRow:
    txn_date: str
    insider: str
    role: str | None
    code: str
    acquired_disposed: str | None
    shares: int | None
    price: float | None
    is_10b5_1: bool
    is_derivative: bool
    url: str


CODE_LABELS = {
    "S": "Sale",
    "P": "Purchase",
    "M": "Option exercise",
    "F": "Tax withholding",
    "A": "Grant/award",
    "G": "Gift",
    "C": "Conversion",
    "X": "Exercise",
}


def insiders_for(engine: Engine, symbol: str, limit: int = 60) -> list[InsiderRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                insider_txns.c.txn_date,
                insider_txns.c.insider,
                insider_txns.c.role,
                insider_txns.c.code,
                insider_txns.c.acquired_disposed,
                insider_txns.c.shares,
                insider_txns.c.price,
                insider_txns.c.is_10b5_1,
                insider_txns.c.is_derivative,
                filings.c.url,
            )
            .join(filings, filings.c.accession == insider_txns.c.accession)
            .where(insider_txns.c.symbol == symbol)
            .order_by(insider_txns.c.txn_date.desc(), insider_txns.c.id.desc())
            .limit(limit)
        ).all()
    return [
        InsiderRow(d, who, role, code, ad, sh, px, bool(plan), bool(der), url)
        for d, who, role, code, ad, sh, px, plan, der, url in rows
    ]


@dataclass(frozen=True)
class CapitalRow:
    as_of: str
    instrument: str
    amount: Decimal | None
    shares_underlying: int | None
    strike: Decimal | None
    source: str
    concept: str | None
    excerpt: str | None
    url: str | None


def capital_for(engine: Engine, symbol: str, limit: int = 40) -> list[CapitalRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                capital_structure.c.as_of,
                capital_structure.c.instrument,
                capital_structure.c.amount_micros,
                capital_structure.c.shares_underlying,
                capital_structure.c.strike_micros,
                capital_structure.c.source,
                capital_structure.c.concept,
                capital_structure.c.excerpt,
                filings.c.url,
            )
            .select_from(
                capital_structure.outerjoin(
                    filings, filings.c.accession == capital_structure.c.source_accession
                )
            )
            .where(capital_structure.c.symbol == symbol)
            .order_by(capital_structure.c.as_of.desc(), capital_structure.c.instrument)
            .limit(limit)
        ).all()
    return [CapitalRow(*r) for r in rows]


@dataclass(frozen=True)
class LockupRow:
    prospectus_date: str
    lockup_days: int
    expiry_date: str
    early_release_possible: bool
    excerpt: str
    url: str


def lockups_for(engine: Engine, symbol: str) -> list[LockupRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                lockups.c.prospectus_date,
                lockups.c.lockup_days,
                lockups.c.expiry_date,
                lockups.c.early_release_possible,
                lockups.c.excerpt,
                filings.c.url,
            )
            .join(filings, filings.c.accession == lockups.c.accession)
            .where(lockups.c.symbol == symbol)
            .order_by(lockups.c.expiry_date.desc())
        ).all()
    return [LockupRow(p, n, e, bool(er), x, u) for p, n, e, er, x, u in rows]


@dataclass(frozen=True)
class EarningsRow:
    date: str
    status: str
    source: str
    url: str | None


def earnings_for(engine: Engine, symbol: str, today: date, past: int = 4) -> list[EarningsRow]:
    """Upcoming dates plus the last `past` reported ones, ascending."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                earnings_calendar.c.date,
                earnings_calendar.c.status,
                earnings_calendar.c.source,
                earnings_calendar.c.source_url,
            )
            .where(earnings_calendar.c.symbol == symbol)
            .order_by(earnings_calendar.c.date)
        ).all()
    t = today.isoformat()
    upcoming = [EarningsRow(*r) for r in rows if r[0] >= t]
    reported = [EarningsRow(*r) for r in rows if r[0] < t][-past:]
    return reported + upcoming


def dilution_series(engine: Engine, symbol: str) -> dict[str, list[tuple[str, int]]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                fundamentals_q.c.concept, fundamentals_q.c.period_end, fundamentals_q.c.value_int
            )
            .where(
                fundamentals_q.c.symbol == symbol,
                fundamentals_q.c.concept.in_(DILUTION_CONCEPTS),
                fundamentals_q.c.value_int.is_not(None),
                # instants, plus quarterly weighted averages (not FY averages)
                fundamentals_q.c.period_days < 120,
            )
            .order_by(fundamentals_q.c.concept, fundamentals_q.c.period_end)
        ).all()
    out: dict[str, list[tuple[str, int]]] = {}
    for concept, d, v in rows:
        out.setdefault(CONCEPT_LABELS[concept], []).append((d, v))
    return out


@dataclass(frozen=True)
class RiskEvent:
    published_at: str
    symbol: str
    title: str
    category: str
    materiality: int
    url: str


def recent_events(
    engine: Engine,
    days: int,
    now: datetime | None = None,
    classes: tuple[str, ...] = ("RISK",),
    symbol: str | None = None,
    limit: int = 30,
) -> list[RiskEvent]:
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = (
        select(
            events.c.published_at,
            event_tickers.c.symbol,
            events.c.title,
            event_classifications.c.category,
            event_classifications.c.materiality,
            events.c.url,
        )
        .join(event_tickers, event_tickers.c.event_id == events.c.id)
        .join(event_classifications, event_classifications.c.event_id == events.c.id)
        .where(
            events.c.published_at >= since,
            events.c.quarantined == 0,
            event_classifications.c["class"].in_(classes),
        )
        .order_by(events.c.published_at.desc())
        .limit(limit)
    )
    if symbol is not None:
        q = q.where(event_tickers.c.symbol == symbol)
    with engine.connect() as conn:
        return [RiskEvent(*r) for r in conn.execute(q).all()]


@dataclass(frozen=True)
class SecFreshness:
    edgar_last_ok: str | None
    edgar_stale: bool
    earnings_last_ok: str | None
    earnings_stale: bool


def sec_freshness(engine: Engine, now: datetime | None = None) -> SecFreshness:
    now = now or datetime.now(UTC)
    e = last_ok_finished(engine, "edgar")
    c = last_ok_finished(engine, "earnings_calendar")
    return SecFreshness(
        e, job_stale(e, now, EDGAR_JOB_MAX_AGE), c, job_stale(c, now, EARNINGS_JOB_MAX_AGE)
    )
