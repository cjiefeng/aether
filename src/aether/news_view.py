"""Read-only queries for the News page and the ticker page's news card (M6). Callers pass the
`mode=ro` engine; nothing here writes. Titles and excerpts are untrusted and are rendered only
through autoescape and the `extlink` filter (S5)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Engine, func, select

from aether.db.models import (
    event_classifications,
    event_sources,
    event_tickers,
    events,
    feed_state,
    llm_calls,
    research_runs,
)
from aether.db.types import micros_sum, micros_to_decimal, to_iso
from aether.llm.pricing import OWN_BUDGET_PURPOSES, sgt_day_start
from aether.market import last_ok_finished

NEWS_ORIGINS = ("rss", "web_search")
ORIGIN_LABELS = {"rss": "RSS", "web_search": "Research"}
RSS_STALE_AFTER = timedelta(hours=3)


@dataclass(frozen=True)
class NewsRow:
    id: int
    published_at: str
    title: str
    url: str
    domain: str
    tier: str
    independent: int
    sources: int
    syndicated: int
    origin: str
    excerpt: str | None
    symbols: tuple[str, ...]
    classified: bool
    date_retrieved: bool
    cls: str | None = None  # M7
    materiality: int | None = None


def news_rows(
    engine: Engine, *, symbol: str | None = None, origin: str | None = None, limit: int = 200
) -> list[NewsRow]:
    q = (
        select(
            events.c.id,
            events.c.published_at,
            events.c.title,
            events.c.url,
            events.c.source_domain,
            events.c.trust_tier,
            events.c.independent_source_count,
            events.c.origin,
            events.c.excerpt,
            events.c.raw,
            event_classifications.c.event_id.label("classified"),
            event_classifications.c["class"].label("cls"),
            event_classifications.c.materiality,
        )
        .outerjoin(event_classifications, event_classifications.c.event_id == events.c.id)
        .where(events.c.origin.in_(NEWS_ORIGINS), events.c.quarantined == 0)
        .order_by(events.c.published_at.desc(), events.c.id.desc())
        .limit(limit)
    )
    if origin in NEWS_ORIGINS:
        q = q.where(events.c.origin == origin)
    if symbol:
        q = q.where(
            events.c.id.in_(
                select(event_tickers.c.event_id).where(event_tickers.c.symbol == symbol)
            )
        )
    with engine.connect() as conn:
        rows = conn.execute(q).all()
        ids = [r.id for r in rows]
        syms: dict[int, list[str]] = defaultdict(list)
        counts: dict[int, tuple[int, int]] = {}
        if ids:
            for eid, s in conn.execute(
                select(event_tickers.c.event_id, event_tickers.c.symbol)
                .where(event_tickers.c.event_id.in_(ids))
                .order_by(event_tickers.c.symbol)
            ):
                syms[eid].append(s)
            for eid, n, synd in conn.execute(
                select(
                    event_sources.c.event_id,
                    func.count(),
                    func.coalesce(func.sum(event_sources.c.syndicated), 0),
                )
                .where(event_sources.c.event_id.in_(ids))
                .group_by(event_sources.c.event_id)
            ):
                counts[eid] = (int(n), int(synd))
    return [
        NewsRow(
            id=r.id,
            published_at=r.published_at,
            title=r.title,
            url=r.url,
            domain=r.source_domain,
            tier=r.trust_tier,
            independent=r.independent_source_count,
            sources=counts.get(r.id, (1, 0))[0],
            syndicated=counts.get(r.id, (1, 0))[1],
            origin=r.origin,
            excerpt=r.excerpt,
            symbols=tuple(syms.get(r.id, ())),
            classified=r.classified is not None,
            date_retrieved='"date_source": "retrieved"' in (r.raw or ""),
            cls=r.cls,
            materiality=r.materiality,
        )
        for r in rows
    ]


@dataclass(frozen=True)
class Spend:
    today: Decimal
    budget: Decimal
    batch_total: Decimal
    calls_today: int
    refused_today: int

    @property
    def pct(self) -> int:
        return int(self.today / self.budget * 100) if self.budget > 0 else 0


def llm_spend(engine: Engine, budget: Decimal, now: datetime | None = None) -> Spend:
    since = to_iso(sgt_day_start(now or datetime.now(UTC)))
    with engine.connect() as conn:
        today, calls, refused = conn.execute(
            select(
                micros_sum(llm_calls.c.cost_micros),
                func.count().filter(llm_calls.c.status == "ok"),
                func.count().filter(llm_calls.c.status == "budget_refused"),
            ).where(
                llm_calls.c.created_at >= since,
                llm_calls.c.batch == 0,
                llm_calls.c.purpose.not_in(OWN_BUDGET_PURPOSES),
            )
        ).one()
        batch = conn.execute(
            select(micros_sum(llm_calls.c.cost_micros)).where(llm_calls.c.batch == 1)
        ).scalar_one()
    return Spend(
        micros_to_decimal(int(today)),
        budget,
        micros_to_decimal(int(batch)),
        int(calls),
        int(refused),
    )


@dataclass(frozen=True)
class RunRow:
    id: int
    kind: str
    symbol: str
    window: str
    status: str
    items_found: int
    events_new: int
    cost: Decimal
    created_at: str
    error: str | None


def research_runs_rows(engine: Engine, limit: int = 20) -> list[RunRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(research_runs)
            .where(research_runs.c.kind == "sweep")
            .order_by(research_runs.c.id.desc())
            .limit(limit)
        ).all()
    return [
        RunRow(
            r.id,
            r.kind,
            r.symbol,
            f"{r.window_start} to {r.window_end}",
            r.status,
            r.items_found,
            r.events_new,
            r.cost_micros,
            r.created_at,
            r.error,
        )
        for r in rows
    ]


@dataclass(frozen=True)
class BackfillSummary:
    total: int
    by_status: dict[str, int]
    events_new: int
    cost: Decimal
    batch_id: str | None


def backfill_summary(engine: Engine) -> BackfillSummary | None:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                research_runs.c.status,
                research_runs.c.events_new,
                research_runs.c.cost_micros,
                research_runs.c.batch_id,
            ).where(research_runs.c.kind == "backfill")
        ).all()
    if not rows:
        return None
    by: dict[str, int] = defaultdict(int)
    for r in rows:
        by[r.status] += 1
    return BackfillSummary(
        total=len(rows),
        by_status=dict(sorted(by.items())),
        events_new=sum(r.events_new for r in rows),
        cost=sum((r.cost_micros for r in rows), Decimal(0)),
        batch_id=next((r.batch_id for r in rows if r.batch_id), None),
    )


def feeds(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as conn:
        rows = conn.execute(select(feed_state).order_by(feed_state.c.feed_id)).all()
    return [dict(r._mapping) for r in rows]


def rss_stale(engine: Engine, now: datetime | None = None) -> str | None:
    """'stale since …' text when the news_rss job hasn't succeeded recently, else None."""
    last = last_ok_finished(engine, "news_rss")
    if last is None:
        return "The RSS job hasn't run yet."
    if (now or datetime.now(UTC)) - datetime.fromisoformat(last) > RSS_STALE_AFTER:
        return f"RSS news stale since {last}."
    return None
