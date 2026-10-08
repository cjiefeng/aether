"""Which events escalate, and whether the caps allow it (spec §5.2.5; M11, tightened in M13).
Reads and pure functions.

Triggers (M13, post-cap, **RISK class only**; SIGNAL and NOISE never escalate):
- materiality >= `min_materiality` (5) → `materiality_5`;
- materiality >= `severe_min_materiality` (4) in a severe category → `severe_category`, where
  - `going_concern`, `short_report`, `guidance_cut` qualify as they are;
  - `delisting_or_compliance` only when the M5 overlay parser read the event's filing as a
    listing-deficiency notice (8-K Item 3.01, `listing_notice == "deficiency"`) or as a removal
    of the **common stock** (Form 25 / 25-NSE / 15-12B / 15-12G, `covers_common`); warrants,
    units, voluntary transfers and 8-K 5.01 don't;
  - `dilution` only when the parsed offering is >= `large_dilution_pct` of fully diluted shares.
    An offering whose size can't be parsed alerts but doesn't escalate.
Routine filings (shelves, small supplements, warrant Form 25s, 8-K 3.02, NT filings) alert only.
Quarantined or injection-suspected events never escalate, and only events published within the
alert lookback window are considered, so a backfill can't flood the caps. Only tickers that get
conclusions are escalated.

Caps: at most `max_per_day` escalations per SGT day, one per ticker per `cooldown_hours`, and
the escalation sub-budget (`budget`). Every non-refused escalation counts. A refusal is final.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Connection, Engine, exists, select

from aether.config import EscalationParams
from aether.db.models import (
    escalations,
    event_classifications,
    event_sources,
    event_tickers,
    events,
    filings,
    prices_daily,
)
from aether.db.types import to_iso
from aether.llm.pricing import sgt_day_start
from aether.score.fundamentals import common_shares, fully_diluted_at, load_facts

DELISTING_FORMS = ("25", "25-NSE", "15-12B", "15-12G")
EIGHT_K = ("8-K", "8-K/A")
PLAIN_SEVERE = ("going_concern", "short_report", "guidance_cut")


@dataclass(frozen=True)
class Candidate:
    event_id: int
    symbol: str
    trigger: str  # 'materiality_5' | 'severe_category'
    cls: str
    category: str
    materiality: int
    trust_tier: str
    has_t1: bool  # the event or one of its sources is T1: no verification search
    reason: str  # why it escalates, for the message and the escalation detail
    title: str
    url: str
    source_domain: str
    published_at: str


@dataclass(frozen=True)
class Recent:
    """A non-refused escalation (counts against the caps)."""

    symbol: str
    created_at: str


@dataclass(frozen=True)
class OfferingSize:
    shares: int
    fd_shares: int
    pct: float
    basis: str  # how the size was read, shown in the message


def _filing(
    conn: Connection, accession: str | None
) -> tuple[str, list[str], dict[str, object], str] | None:
    """(form, items, parsed summary, filed_at) of the event's filing, if stored and parsed."""
    if not accession:
        return None
    r = conn.execute(
        select(filings.c.form, filings.c["items"], filings.c.parsed, filings.c.filed_at).where(
            filings.c.accession == accession
        )
    ).first()
    if r is None or not r.parsed:
        return None
    return r.form, json.loads(r._mapping["items"]), json.loads(r.parsed), r.filed_at


def _last_close(conn: Connection, symbol: str, day: str) -> float | None:
    c = conn.execute(
        select(prices_daily.c.c)
        .where(prices_daily.c.symbol == symbol, prices_daily.c.d <= day)
        .order_by(prices_daily.c.d.desc())
        .limit(1)
    ).scalar()
    return float(c) if c is not None and c > 0 else None


def offering_size(conn: Connection, symbol: str, accession: str | None) -> OfferingSize | None:
    """The parsed offering as a share of fully diluted shares (XBRL, as filed by the offering
    date). Cover shares when stated; else an ATM's dollar size at the last close on or before the
    filing date. None when either side is missing: nothing is guessed."""
    f = _filing(conn, accession)
    if f is None:
        return None
    _form, _items, parsed, filed_at = f
    day = filed_at[:10]
    offering = parsed.get("offering")
    atm = parsed.get("atm")
    if isinstance(offering, dict) and int(offering.get("shares") or 0) > 0:
        shares = int(offering["shares"])
        basis = f"{shares:,} shares offered (prospectus cover)"
    elif isinstance(atm, dict) and atm.get("amount"):
        close = _last_close(conn, symbol, day)
        if close is None:
            return None
        amount = Decimal(str(atm["amount"]))
        shares = int(amount / Decimal(repr(close)))
        basis = f"ATM up to ${amount:,.0f} ≈ {shares:,} shares at the ${close:,.2f} close"
    else:
        return None
    facts = load_facts(conn, symbol, date.fromisoformat(day))
    common = common_shares(facts)
    if common is None:
        return None
    fd = fully_diluted_at(facts, common).total
    if fd <= 0:
        return None
    return OfferingSize(shares, fd, shares / fd, basis)


def severe_reason(
    conn: Connection, symbol: str, category: str, accession: str | None, p: EscalationParams
) -> str | None:
    """Why a materiality-4 RISK event in a severe category escalates, or None (alert only)."""
    if category not in p.severe_categories:
        return None
    label = category.replace("_", " ")
    if category in PLAIN_SEVERE:
        return label
    if category == "delisting_or_compliance":
        f = _filing(conn, accession)
        if f is None:
            return None
        form, items, parsed, _ = f
        if form in DELISTING_FORMS and parsed.get("covers_common") is True:
            return f"Form {form} removes the common stock"
        if form in EIGHT_K and "3.01" in items and parsed.get("listing_notice") == "deficiency":
            return "listing-deficiency notice (8-K 3.01)"
        return None
    if category == "dilution":
        size = offering_size(conn, symbol, accession)
        if size is None or size.pct < p.large_dilution_pct:
            return None
        return (
            f"offering ≈ {size.pct:.1%} of {size.fd_shares:,} fully diluted shares ({size.basis})"
        )
    return None


def refusal(
    symbol: str,
    now: datetime,
    recent: Sequence[Recent],
    *,
    max_per_day: int,
    cooldown: timedelta,
    budget_left: bool = True,
) -> str | None:
    """None if the escalation may run, else the cap that refuses it."""
    day = to_iso(sgt_day_start(now))
    if sum(1 for r in recent if r.created_at >= day) >= max_per_day:
        return "daily_cap"
    since = to_iso(now - cooldown)
    if any(r.symbol == symbol and r.created_at > since for r in recent):
        return "ticker_cooldown"
    if not budget_left:
        return "budget"
    return None


def candidates(
    engine: Engine,
    p: EscalationParams,
    symbols: Collection[str],
    now: datetime,
    lookback_days: int,
) -> list[Candidate]:
    since = to_iso(now - timedelta(days=lookback_days))
    t1_source = exists().where(
        event_sources.c.event_id == events.c.id, event_sources.c.trust_tier == "T1"
    )
    out = []
    with engine.connect() as conn:
        done = {
            (eid, sym)
            for eid, sym in conn.execute(select(escalations.c.event_id, escalations.c.symbol))
        }
        rows = conn.execute(
            select(
                events.c.id,
                events.c.title,
                events.c.url,
                events.c.source_domain,
                events.c.published_at,
                events.c.trust_tier,
                events.c.accession,
                t1_source.label("t1_source"),
                event_classifications.c["class"],
                event_classifications.c.category,
                event_classifications.c.materiality,
                event_tickers.c.symbol,
            )
            .join(event_classifications, event_classifications.c.event_id == events.c.id)
            .join(event_tickers, event_tickers.c.event_id == events.c.id)
            .where(
                event_classifications.c["class"] == "RISK",
                event_classifications.c.materiality >= p.severe_min_materiality,
                events.c.quarantined == 0,
                events.c.injection_suspected == 0,
                events.c.published_at >= since,
                event_tickers.c.symbol.in_(list(symbols)),
            )
            .order_by(events.c.published_at, events.c.id, event_tickers.c.symbol)
        ).all()
        for r in rows:
            if (r.id, r.symbol) in done:
                continue
            if r.materiality >= p.min_materiality:
                trigger, reason = "materiality_5", f"materiality {r.materiality}"
            else:
                why = severe_reason(conn, r.symbol, r.category, r.accession, p)
                if why is None:
                    continue
                trigger, reason = "severe_category", why
            out.append(
                Candidate(
                    event_id=r.id,
                    symbol=r.symbol,
                    trigger=trigger,
                    cls=r._mapping["class"],
                    category=r.category,
                    materiality=r.materiality,
                    trust_tier=r.trust_tier,
                    has_t1=r.trust_tier == "T1" or bool(r.t1_source),
                    reason=reason,
                    title=r.title,
                    url=r.url,
                    source_domain=r.source_domain,
                    published_at=r.published_at,
                )
            )
    return out


def recent(engine: Engine, now: datetime, cooldown: timedelta) -> list[Recent]:
    """Non-refused escalations since the earlier of SGT midnight and now - cooldown."""
    since = to_iso(min(sgt_day_start(now), now - cooldown))
    with engine.connect() as conn:
        return [
            Recent(sym, at)
            for sym, at in conn.execute(
                select(escalations.c.symbol, escalations.c.created_at).where(
                    escalations.c.status != "refused", escalations.c.created_at >= since
                )
            ).all()
        ]
