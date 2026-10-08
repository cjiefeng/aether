"""Read-only queries for the Holdings page and the Overview drift card. Callers pass the
dashboard's `mode=ro` engine. Nothing here writes or calls an LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, select

from aether.config import SLEEVE_TYPES
from aether.db.models import (
    commands,
    event_classifications,
    event_tickers,
    events,
    rebalance_plans,
    review_packs,
    tickers,
)
from aether.market import job_stale, last_ok_finished, symbol_stale
from aether.portfolio.holdings import (
    Holdings,
    PortfolioSettings,
    load_holdings,
    load_settings,
    universe_symbols,
)
from aether.portfolio.overlay import chain_text
from aether.portfolio.publish import Published, last_prices, latest_published, next_publish
from aether.portfolio.rebalance import drift_summary
from aether.review.pack import latest_fx

REBALANCE_JOB_MAX_AGE = timedelta(hours=30)
TIGER_SYNC_MAX_AGE = timedelta(hours=30)
OWNER_COMMANDS = (
    "update_holdings",
    "update_portfolio_settings",
    "sync_holdings",
    "publish_targets",
)
OFF_CYCLE_LOOKBACK = timedelta(days=30)


@dataclass(frozen=True)
class PriceInfo:
    d: str
    c: float
    stale: bool


@dataclass(frozen=True)
class HoldingsPage:
    settings: PortfolioSettings
    holdings: Holdings
    universe: list[str]
    prices: dict[str, PriceInfo]
    plan: dict[str, Any] | None
    plan_as_of: str | None
    plan_created_at: str | None
    targets: Published | None
    chains: list[dict[str, Any]]
    next_publish: str | None
    off_cycle: list[dict[str, Any]]
    fx: tuple[str, float] | None
    pending: int
    rebalance_last_ok: str | None
    rebalance_stale: bool
    tiger: dict[str, Any] | None
    tiger_stale: bool


def _latest_plan(engine: Engine, profile: str) -> tuple[dict[str, Any], str, str] | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(rebalance_plans.c.plan, rebalance_plans.c.as_of, rebalance_plans.c.created_at)
            .where(rebalance_plans.c.profile == profile)
            .order_by(rebalance_plans.c.as_of.desc())
            .limit(1)
        ).first()
    return None if row is None else (json.loads(row.plan), row.as_of, row.created_at)


def off_cycle_events(engine: Engine, since: str, min_materiality: int = 4) -> list[dict[str, Any]]:
    """Material events on pure-plays since the last publish: candidates for an off-cycle
    review (shown next to "Publish targets now")."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                events.c.id,
                events.c.title,
                events.c.published_at,
                event_tickers.c.symbol,
                event_classifications.c.category,
                event_classifications.c.materiality,
            )
            .join(event_tickers, event_tickers.c.event_id == events.c.id)
            .join(tickers, tickers.c.symbol == event_tickers.c.symbol)
            .join(event_classifications, event_classifications.c.event_id == events.c.id)
            .where(
                tickers.c.type.in_(SLEEVE_TYPES),
                events.c.quarantined == 0,
                event_classifications.c.materiality >= min_materiality,
                events.c.published_at >= since,
            )
            .order_by(events.c.published_at.desc(), events.c.id.desc())
            .limit(10)
        ).all()
    return [dict(r._mapping) for r in rows]


def pending_owner_commands(engine: Engine) -> int:
    with engine.connect() as conn:
        n = conn.execute(
            select(func.count())
            .select_from(commands)
            .where(commands.c.status == "pending", commands.c.kind.in_(OWNER_COMMANDS))
        ).scalar_one()
    return int(n)


def holdings_page(engine: Engine, today: date, now: datetime | None = None) -> HoldingsPage:
    now = now or datetime.now(UTC)
    settings = load_settings(engine)
    h = load_holdings(engine)
    universe = universe_symbols(engine)
    with engine.connect() as conn:
        raw_prices = last_prices(conn, sorted(set(universe) | set(h.positions)))
    prices = {s: PriceInfo(d, float(c), symbol_stale(d, today)) for s, (d, c) in raw_prices.items()}
    latest = _latest_plan(engine, settings.selected_profile)
    with engine.connect() as conn:
        target = latest_published(conn, settings.selected_profile)
    chains = [
        {**c, "text": chain_text(c)}
        for c in (target.adjustments.get("chain", []) if target else [])
    ]
    since = (
        target.published_at if target else (now - OFF_CYCLE_LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    rb_last = last_ok_finished(engine, "rebalance")
    tiger = settings.tiger_sync
    tiger_stale = settings.holdings_source == "tiger" and (
        tiger is None
        or bool(tiger.get("error"))
        or job_stale(tiger.get("last_ok"), now, TIGER_SYNC_MAX_AGE)
    )
    return HoldingsPage(
        settings=settings,
        holdings=h,
        universe=universe,
        prices=prices,
        plan=latest[0] if latest else None,
        plan_as_of=latest[1] if latest else None,
        plan_created_at=latest[2] if latest else None,
        targets=target,
        chains=chains,
        next_publish=next_publish(date.fromisoformat(target.as_of)).isoformat() if target else None,
        off_cycle=off_cycle_events(engine, since),
        fx=latest_fx(engine),
        pending=pending_owner_commands(engine),
        rebalance_last_ok=rb_last,
        rebalance_stale=job_stale(rb_last, now, REBALANCE_JOB_MAX_AGE),
        tiger=tiger,
        tiger_stale=tiger_stale,
    )


@dataclass(frozen=True)
class DriftCard:
    profile: str
    as_of: str
    rows: list[dict[str, Any]]


def drift_card(engine: Engine) -> DriftCard | None:
    """Overview card: drift vs the selected profile, only when holdings are saved."""
    if load_holdings(engine).empty:
        return None
    settings = load_settings(engine)
    latest = _latest_plan(engine, settings.selected_profile)
    if latest is None:
        return None
    plan, as_of, _ = latest
    return DriftCard(settings.selected_profile, as_of, drift_summary(plan))


@dataclass(frozen=True)
class ReviewRow:
    as_of: str
    month: str
    status: str
    error: str | None
    created_at: str
    payload: dict[str, Any]
    telegram_text: str | None


def review_packs_view(engine: Engine, limit: int = 24) -> list[ReviewRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(review_packs).order_by(review_packs.c.as_of.desc()).limit(limit)
        ).all()
    return [
        ReviewRow(
            r.as_of,
            r.month,
            r.status,
            r.error,
            r.created_at,
            json.loads(r.payload),
            r.telegram_text,
        )
        for r in rows
    ]
