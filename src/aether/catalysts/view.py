"""Read-only catalyst and short-interest views for the dashboard (spec §8: Catalysts page,
Overview timeline, ticker page). The page path never writes."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, select

from aether.db.models import catalysts, events, facts, short_interest

KIND_LABELS = {
    "roadmap": "Roadmap",
    "program": "Program",
    "earnings": "Earnings",
    "lockup": "Lock-up",
    "regulatory": "Regulatory",
}


def _query() -> Any:
    return (
        select(
            catalysts,
            facts.c.status.label("fact_status"),
            events.c.title.label("event_title"),
            events.c.url.label("event_url"),
        )
        .outerjoin(facts, facts.c.id == catalysts.c.fact_id)
        .outerjoin(events, events.c.id == catalysts.c.resolved_by_event_id)
    )


def _row(r: Any) -> dict[str, Any]:
    d = dict(r._mapping)
    d["keywords"] = json.loads(d["keywords"])
    d["kind_label"] = KIND_LABELS.get(d["kind"], d["kind"])
    return d


def upcoming(
    engine: Engine, today: date, days: int = 365, symbol: str | None = None
) -> list[dict[str, Any]]:
    """Unresolved catalysts whose window starts within `days` (or is open now)."""
    horizon = (today + timedelta(days=days)).isoformat()
    q = _query().where(
        catalysts.c.status == "upcoming",
        catalysts.c.window_start <= horizon,
    )
    if symbol is not None:
        q = q.where(catalysts.c.symbol == symbol)
    with engine.connect() as conn:
        rows = conn.execute(
            q.order_by(catalysts.c.window_start, catalysts.c.symbol, catalysts.c.id)
        ).all()
    out = [_row(r) for r in rows]
    t = today.isoformat()
    for r in out:
        end = r["window_end"] or r["window_start"]
        r["overdue"] = end < t
    return out


def resolved(engine: Engine, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
    """Hit / slipped / cancelled catalysts, newest first."""
    q = _query().where(catalysts.c.status != "upcoming")
    if symbol is not None:
        q = q.where(catalysts.c.symbol == symbol)
    with engine.connect() as conn:
        rows = conn.execute(
            q.order_by(catalysts.c.window_start.desc(), catalysts.c.id.desc()).limit(limit)
        ).all()
    return [_row(r) for r in rows]


def hit_slip_counts(engine: Engine) -> dict[str, dict[str, int]]:
    """Per symbol (or 'theme'): counts by status, excluding upcoming."""
    out: dict[str, dict[str, int]] = {}
    with engine.connect() as conn:
        for sym, status in conn.execute(
            select(catalysts.c.symbol, catalysts.c.status).where(catalysts.c.status != "upcoming")
        ):
            key = sym or "theme"
            out.setdefault(key, {"hit": 0, "slipped": 0, "cancelled": 0})[status] += 1
    return dict(sorted(out.items()))


def timeline(engine: Engine, today: date, days: int = 365) -> list[dict[str, Any]]:
    """Chart data: one point per upcoming catalyst (window start, clipped to today)."""
    t = today.isoformat()
    out = []
    for r in upcoming(engine, today, days):
        out.append(
            {
                "id": r["id"],
                "symbol": r["symbol"] or "theme",
                "kind": r["kind"],
                "title": r["title"],
                "start": max(r["window_start"], t),
                "end": r["window_end"],
                "fact_status": r["fact_status"],
            }
        )
    return out


def short_interest_rows(engine: Engine, symbol: str, limit: int = 12) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(short_interest)
            .where(short_interest.c.symbol == symbol)
            .order_by(short_interest.c.settlement_date.desc())
            .limit(limit)
        ).all()
    out = [dict(r._mapping) for r in rows]
    for i, r in enumerate(out):
        prev = out[i + 1]["pct_shares_out"] if i + 1 < len(out) else None
        cur = r["pct_shares_out"]
        r["change_pp"] = None if cur is None or prev is None else round(cur - prev, 2)
    return out
