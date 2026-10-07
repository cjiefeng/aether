"""Which events escalate, and whether the caps allow it (spec §5.2.5). Reads and pure functions.

Triggers (post-cap): materiality >= `min_materiality` (any class), or a RISK event on a T1 source
with materiality >= `t1_risk_min_materiality`. Quarantined or injection-suspected events never
escalate, and only events published within the alert lookback window are considered, so the
first backfill can't flood the caps. Only tickers that get conclusions are escalated.

Caps: at most `max_per_day` escalations per SGT day and one per ticker per `cooldown_hours`.
Every non-refused escalation counts, including ones the budget guard stopped. A refusal is final.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, select

from aether.config import EscalationParams
from aether.db.models import escalations, event_classifications, event_tickers, events
from aether.db.types import to_iso
from aether.llm.pricing import sgt_day_start


@dataclass(frozen=True)
class Candidate:
    event_id: int
    symbol: str
    trigger: str  # 'materiality' | 't1_risk'
    cls: str
    category: str
    materiality: int
    trust_tier: str
    title: str
    url: str
    source_domain: str
    published_at: str


@dataclass(frozen=True)
class Recent:
    """A non-refused escalation (counts against the caps)."""

    symbol: str
    created_at: str


def trigger_for(cls: str, materiality: int, tier: str, p: EscalationParams) -> str | None:
    if materiality >= p.min_materiality:
        return "materiality"
    if cls == "RISK" and tier == "T1" and materiality >= p.t1_risk_min_materiality:
        return "t1_risk"
    return None


def refusal(
    symbol: str,
    now: datetime,
    recent: Sequence[Recent],
    *,
    max_per_day: int,
    cooldown: timedelta,
) -> str | None:
    """None if the escalation may run, else the cap that refuses it."""
    day = to_iso(sgt_day_start(now))
    if sum(1 for r in recent if r.created_at >= day) >= max_per_day:
        return "daily_cap"
    since = to_iso(now - cooldown)
    if any(r.symbol == symbol and r.created_at > since for r in recent):
        return "ticker_cooldown"
    return None


def candidates(
    engine: Engine,
    p: EscalationParams,
    symbols: Collection[str],
    now: datetime,
    lookback_days: int,
) -> list[Candidate]:
    since = to_iso(now - timedelta(days=lookback_days))
    low = min(p.min_materiality, p.t1_risk_min_materiality)
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
                event_classifications.c["class"],
                event_classifications.c.category,
                event_classifications.c.materiality,
                event_tickers.c.symbol,
            )
            .join(event_classifications, event_classifications.c.event_id == events.c.id)
            .join(event_tickers, event_tickers.c.event_id == events.c.id)
            .where(
                event_classifications.c.materiality >= low,
                events.c.quarantined == 0,
                events.c.injection_suspected == 0,
                events.c.published_at >= since,
                event_tickers.c.symbol.in_(list(symbols)),
            )
            .order_by(events.c.published_at, events.c.id, event_tickers.c.symbol)
        ).all()
    out = []
    for r in rows:
        cls = r._mapping["class"]
        trig = trigger_for(cls, r.materiality, r.trust_tier, p)
        if trig is None or (r.id, r.symbol) in done:
            continue
        out.append(
            Candidate(
                event_id=r.id,
                symbol=r.symbol,
                trigger=trig,
                cls=cls,
                category=r.category,
                materiality=r.materiality,
                trust_tier=r.trust_tier,
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
