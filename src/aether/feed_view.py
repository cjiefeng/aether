"""Read-only queries for the Feed page (M7, spec §8 item 3). Callers pass the `mode=ro` engine;
nothing here writes. Titles, excerpts, rationales and quotes are untrusted (or model output) and are
rendered only through autoescape and the `extlink` filter (S5)."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, and_, func, or_, select

from aether.config import CATEGORY_CLASS
from aether.db.models import (
    classify_state,
    eval_runs,
    event_classifications,
    event_sources,
    event_tickers,
    events,
)
from aether.db.types import to_iso

CLASSES = ("SIGNAL", "NOISE", "RISK")
TIERS = ("T1", "T2", "T3")
NEWS_ORIGINS = ("rss", "web_search", "manual")
ORIGIN_LABELS = {
    "rss": "RSS",
    "web_search": "Research",
    "edgar": "SEC EDGAR",
    "manual": "Manual",
    "finra": "FINRA",
}
COUNT_DAYS = 30


@dataclass(frozen=True)
class FeedFilters:
    cls: str | None = None
    category: str | None = None
    symbol: str | None = None
    min_materiality: int = 1
    tier: str | None = None
    show_noise: bool = False

    @classmethod
    def parse(
        cls,
        *,
        klass: str,
        category: str,
        symbol: str,
        min_materiality: str,
        tier: str,
        noise: str,
        symbols: list[str],
    ) -> FeedFilters:
        k = klass if klass in CLASSES else None
        cat = category if category in CATEGORY_CLASS else None
        try:
            m = min(5, max(1, int(min_materiality or 1)))
        except ValueError:
            m = 1
        return cls(
            cls=k,
            category=cat,
            symbol=symbol if symbol in symbols else None,
            min_materiality=m,
            tier=tier if tier in TIERS else None,
            # Asking for NOISE (by class or a NOISE category) shows it.
            show_noise=noise == "1" or k == "NOISE" or CATEGORY_CLASS.get(cat or "") == "NOISE",
        )


@dataclass(frozen=True)
class SourceRow:
    url: str
    domain: str
    tier: str
    syndicated: bool


@dataclass(frozen=True)
class FeedRow:
    id: int
    published_at: str
    title: str
    url: str
    domain: str
    tier: str
    origin: str
    excerpt: str | None
    independent: int
    cls: str
    category: str
    materiality: int
    materiality_raw: int
    direction: int
    confidence: float
    rationale: str | None
    evidence_quote: str | None
    rule_id: str | None
    model: str | None
    prompt_version: str | None
    quarantined: bool
    tickers: tuple[tuple[str, int | None], ...]
    sources: tuple[SourceRow, ...]

    @property
    def capped(self) -> bool:
        return self.materiality < self.materiality_raw


def feed_rows(engine: Engine, f: FeedFilters, limit: int = 200) -> list[FeedRow]:
    ec = event_classifications
    q = (
        select(
            events.c.id,
            events.c.published_at,
            events.c.title,
            events.c.url,
            events.c.source_domain,
            events.c.trust_tier,
            events.c.origin,
            events.c.excerpt,
            events.c.independent_source_count,
            events.c.quarantined,
            events.c.injection_suspected,
            ec.c["class"],
            ec.c.category,
            ec.c.materiality,
            ec.c.materiality_raw,
            ec.c.direction,
            ec.c.confidence,
            ec.c.rationale,
            ec.c.evidence_quote,
            ec.c.rule_id,
            ec.c.model,
            ec.c.prompt_version,
        )
        .join(ec, ec.c.event_id == events.c.id)
        .where(ec.c.materiality >= f.min_materiality)
        .order_by(events.c.published_at.desc(), events.c.id.desc())
        .limit(limit)
    )
    if f.cls:
        q = q.where(ec.c["class"] == f.cls)
    if f.category:
        q = q.where(ec.c.category == f.category)
    if not f.show_noise:
        q = q.where(ec.c["class"] != "NOISE")
    if f.tier:
        q = q.where(events.c.trust_tier == f.tier)
    if f.symbol:
        q = q.where(
            events.c.id.in_(
                select(event_tickers.c.event_id).where(event_tickers.c.symbol == f.symbol)
            )
        )
    with engine.connect() as conn:
        rows = conn.execute(q).all()
        ids = [r.id for r in rows]
        tick: dict[int, list[tuple[str, int | None]]] = defaultdict(list)
        srcs: dict[int, list[SourceRow]] = defaultdict(list)
        if ids:
            for eid, sym, d in conn.execute(
                select(event_tickers.c.event_id, event_tickers.c.symbol, event_tickers.c.direction)
                .where(event_tickers.c.event_id.in_(ids))
                .order_by(event_tickers.c.symbol)
            ):
                tick[eid].append((sym, d))
            for eid, url, dom, tier, synd in conn.execute(
                select(
                    event_sources.c.event_id,
                    event_sources.c.url,
                    event_sources.c.domain,
                    event_sources.c.trust_tier,
                    event_sources.c.syndicated,
                )
                .where(event_sources.c.event_id.in_(ids))
                .order_by(event_sources.c.trust_tier, event_sources.c.domain)
            ):
                srcs[eid].append(SourceRow(url, dom, tier, bool(synd)))
    out = []
    for r in rows:
        m = r._mapping
        out.append(
            FeedRow(
                id=r.id,
                published_at=r.published_at,
                title=r.title,
                url=r.url,
                domain=r.source_domain,
                tier=r.trust_tier,
                origin=r.origin,
                excerpt=r.excerpt,
                independent=r.independent_source_count,
                cls=m["class"],
                category=r.category,
                materiality=r.materiality,
                materiality_raw=r.materiality_raw,
                direction=r.direction,
                confidence=r.confidence,
                rationale=r.rationale,
                evidence_quote=r.evidence_quote,
                rule_id=r.rule_id,
                model=r.model,
                prompt_version=r.prompt_version,
                quarantined=bool(r.quarantined or r.injection_suspected),
                tickers=tuple(tick[r.id]),
                sources=tuple(srcs[r.id]),
            )
        )
    return out


@dataclass(frozen=True)
class FeedCounts:
    by_class: dict[str, int]  # non-quarantined, last COUNT_DAYS days
    quarantined: int
    pending: int  # news/research items not yet classified
    batched: int
    failed: int


def feed_counts(engine: Engine, now: datetime | None = None) -> FeedCounts:
    now = now or datetime.now(UTC)
    since = to_iso(now - timedelta(days=COUNT_DAYS))
    ec = event_classifications
    with engine.connect() as conn:
        by_class = dict(
            (str(k), int(v))
            for k, v in conn.execute(
                select(ec.c["class"], func.count())
                .join(events, events.c.id == ec.c.event_id)
                .where(events.c.published_at >= since, events.c.quarantined == 0)
                .group_by(ec.c["class"])
            )
        )
        quarantined = conn.execute(
            select(func.count()).select_from(events).where(events.c.quarantined == 1)
        ).scalar_one()
        states = dict(
            (str(k), int(v))
            for k, v in conn.execute(
                select(classify_state.c.status, func.count()).group_by(classify_state.c.status)
            )
        )
        pending = conn.execute(
            select(func.count())
            .select_from(events.outerjoin(ec, ec.c.event_id == events.c.id))
            .outerjoin(classify_state, classify_state.c.event_id == events.c.id)
            .where(
                events.c.origin.in_(NEWS_ORIGINS),
                ec.c.event_id.is_(None),
                or_(classify_state.c.status.is_(None), classify_state.c.status == "retry"),
            )
        ).scalar_one()
    return FeedCounts(
        by_class={c: by_class.get(c, 0) for c in CLASSES},
        quarantined=int(quarantined),
        pending=int(pending),
        batched=states.get("batched", 0),
        failed=states.get("failed", 0),
    )


@dataclass(frozen=True)
class FailedRow:
    id: int
    published_at: str
    title: str
    url: str
    attempts: int
    error: str | None


def failed_rows(engine: Engine, limit: int = 20) -> list[FailedRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                events.c.id,
                events.c.published_at,
                events.c.title,
                events.c.url,
                classify_state.c.attempts,
                classify_state.c.last_error,
            )
            .join(classify_state, classify_state.c.event_id == events.c.id)
            .where(classify_state.c.status == "failed")
            .order_by(events.c.published_at.desc())
            .limit(limit)
        ).all()
    return [FailedRow(r.id, r.published_at, r.title, r.url, r.attempts, r.last_error) for r in rows]


@dataclass(frozen=True)
class EvalSummary:
    prompt_version: str
    model: str
    created_at: str
    n_items: int
    provisional: bool
    passed: bool
    class_agreement: float
    risk_recall: float | None
    adversarial_flagged: str


def latest_eval(engine: Engine, prompt_version: str) -> EvalSummary | None:
    """The newest eval run for this prompt version, else None."""
    with engine.connect() as conn:
        r = conn.execute(
            select(eval_runs)
            .where(and_(eval_runs.c.prompt_version == prompt_version))
            .order_by(eval_runs.c.created_at.desc())
            .limit(1)
        ).first()
    if r is None:
        return None
    m = json.loads(r.metrics)
    adv = m.get("adversarial", {})
    return EvalSummary(
        prompt_version=r.prompt_version,
        model=r.model,
        created_at=r.created_at,
        n_items=r.n_items,
        provisional=bool(r.provisional),
        passed=bool(r.passed),
        class_agreement=float(m.get("class_agreement", 0.0)),
        risk_recall=m.get("risk_recall"),
        adversarial_flagged=f"{adv.get('flagged', 0)}/{adv.get('n', 0)}",
    )
