"""Deterministic catalyst resolution (spec §11 M8 "auto-resolution from events"). Pure reads.

Rules, applied only to `upcoming` catalysts:

- **earnings**: an EDGAR `earnings_release` event (8-K Item 2.02) on the symbol published within
  ± `earnings_match_days` of the date → `hit`, citing the event.
- **lockup**: the expiry date has arrived → `hit` (resolution `date`).
- **roadmap / program / regulatory** with `resolve_categories`: the earliest classified event that
  is not quarantined or injection-flagged, at or above `min_materiality` after trust-tier caps, in
  one of the catalyst's categories, published in [window_start - lead_days, window_end +
  grace_days], whose title or excerpt names one of the catalyst's keywords (whole words, any
  case). For a watchlist company (ETF / pure-play) the event's tickers must include the symbol or
  be empty (a theme item); for a context ticker such as IBM, which news isn't tagged with, the
  keyword alone ties the event to it.
  roadmap_hit → hit, roadmap_slip → slipped; qbi_stage_change by the event's direction for the
  symbol (+1 hit, -1 slipped, 0 ignored).
- **window passed**: roadmap / program / regulatory with an end date, still unresolved
  `grace_days` after it → `slipped` (resolution `window_passed`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Connection, func, select

from aether.config import CatalystRules
from aether.db.models import catalysts, event_classifications, event_tickers, events, tickers

EVENT_KINDS = ("roadmap", "program", "regulatory")


@dataclass(frozen=True)
class Resolution:
    catalyst_id: int
    status: str  # hit | slipped
    resolution: str  # event | date | window_passed
    event_id: int | None
    note: str


def _keyword_re(keywords: list[str]) -> re.Pattern[str] | None:
    if not keywords:
        return None
    alts = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", re.IGNORECASE)


def _event_status(category: str, direction: int | None) -> str | None:
    if category == "roadmap_hit":
        return "hit"
    if category == "roadmap_slip":
        return "slipped"
    if category == "qbi_stage_change":
        return {1: "hit", -1: "slipped"}.get(direction or 0)
    return None


def _event_match(
    conn: Connection,
    rules: CatalystRules,
    cid: int,
    symbol: str | None,
    scoped: bool,
    window_start: str,
    window_end: str | None,
    keywords: list[str],
    categories: list[str],
) -> Resolution | None:
    kw = _keyword_re(keywords)
    if kw is None or not categories:
        return None
    lo = (date.fromisoformat(window_start) - timedelta(days=rules.lead_days)).isoformat()
    q = (
        select(
            events.c.id,
            events.c.title,
            events.c.excerpt,
            events.c.published_at,
            event_classifications.c.category,
            event_classifications.c.direction,
        )
        .join(event_classifications, event_classifications.c.event_id == events.c.id)
        .where(
            event_classifications.c.category.in_(categories),
            event_classifications.c.materiality >= rules.min_materiality,
            events.c.quarantined == 0,
            events.c.injection_suspected == 0,
            events.c.published_at >= lo,
        )
        .order_by(events.c.published_at, events.c.id)
    )
    if window_end is not None:
        hi = date.fromisoformat(window_end) + timedelta(days=rules.grace_days + 1)
        q = q.where(events.c.published_at < hi.isoformat())
    for ev in conn.execute(q).all():
        tagged = dict(
            conn.execute(
                select(event_tickers.c.symbol, event_tickers.c.direction).where(
                    event_tickers.c.event_id == ev.id
                )
            ).all()
        )
        if scoped and tagged and symbol not in tagged:
            continue
        if not kw.search(f"{ev.title}\n{ev.excerpt or ''}"):
            continue
        direction = tagged.get(symbol) if symbol in tagged else None
        status = _event_status(ev.category, direction if direction is not None else ev.direction)
        if status is None:
            continue
        return Resolution(
            cid,
            status,
            "event",
            ev.id,
            f"{ev.category.replace('_', ' ')} event #{ev.id} ({ev.published_at[:10]})",
        )
    return None


def _earnings_match(
    conn: Connection, rules: CatalystRules, cid: int, symbol: str, d: str
) -> Resolution | None:
    day = date.fromisoformat(d)
    lo = (day - timedelta(days=rules.earnings_match_days)).isoformat()
    hi = (day + timedelta(days=rules.earnings_match_days + 1)).isoformat()
    row = conn.execute(
        select(events.c.id, events.c.published_at)
        .join(event_classifications, event_classifications.c.event_id == events.c.id)
        .join(event_tickers, event_tickers.c.event_id == events.c.id)
        .where(
            event_tickers.c.symbol == symbol,
            events.c.origin == "edgar",
            event_classifications.c.category == "earnings_release",
            events.c.published_at >= lo,
            events.c.published_at < hi,
        )
        .order_by(func.abs(func.julianday(events.c.published_at) - func.julianday(d)))
        .limit(1)
    ).first()
    if row is None:
        return None
    return Resolution(cid, "hit", "event", row.id, f"8-K Item 2.02 filed {row.published_at[:10]}")


def resolve_upcoming(conn: Connection, rules: CatalystRules, today: date) -> list[Resolution]:
    t = today.isoformat()
    out: list[Resolution] = []
    scoped_symbols = set(
        conn.execute(
            select(tickers.c.symbol).where(tickers.c.type.in_(("etf", "pure_play")))
        ).scalars()
    )
    rows = conn.execute(
        select(
            catalysts.c.id,
            catalysts.c.symbol,
            catalysts.c.kind,
            catalysts.c.window_start,
            catalysts.c.window_end,
            catalysts.c.keywords,
            catalysts.c.resolve_categories,
        )
        .where(catalysts.c.status == "upcoming")
        .order_by(catalysts.c.id)
    ).all()
    for r in rows:
        res: Resolution | None = None
        if r.kind == "earnings" and r.symbol is not None:
            res = _earnings_match(conn, rules, r.id, r.symbol, r.window_start)
        elif r.kind == "lockup":
            if r.window_start <= t:
                res = Resolution(r.id, "hit", "date", None, f"expiry date {r.window_start} reached")
        elif r.kind in EVENT_KINDS:
            res = _event_match(
                conn,
                rules,
                r.id,
                r.symbol,
                r.symbol in scoped_symbols,
                r.window_start,
                r.window_end,
                json.loads(r.keywords),
                json.loads(r.resolve_categories),
            )
            if res is None and r.window_end is not None:
                deadline = date.fromisoformat(r.window_end) + timedelta(days=rules.grace_days)
                if today > deadline:
                    res = Resolution(
                        r.id,
                        "slipped",
                        "window_passed",
                        None,
                        f"no resolving event by {deadline.isoformat()} "
                        f"(window end + {rules.grace_days} days)",
                    )
        if res is not None:
            out.append(res)
    return out
