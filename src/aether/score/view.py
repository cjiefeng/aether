"""Read-only queries for the M9 dashboard: scorecards, theme decomposition, reactions, calibration.
Runs on the dashboard's `mode=ro` engine; never writes.

"Market agreed / disagreed" (ticker page): for a complete, non-confounded SIGNAL or RISK reaction
with a non-zero direction, the market agreed if CAR[0,5] has the classifier's sign. For NOISE it
agreed if the move wasn't significant (|z5| < 2). Pending, confounded and undirected rows get no
badge.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select

from aether.db.models import (
    calibration_reports,
    event_classifications,
    event_reactions,
    event_tickers,
    events,
    scorecards,
    theme_decomposition,
)
from aether.market import last_ok_finished

COMPONENT_LABELS = {
    "fundamentals": "Fundamentals",
    "dilution": "Dilution",
    "signal_momentum": "Signal momentum",
    "risk_load": "Risk load",
    "short_interest": "Short interest",
    "catalyst_position": "Catalyst position",
    "noise_ratio": "Noise ratio",
    "price_context": "Price context",
    "market_reaction": "Market reaction",
}
FD_LABELS = {
    "warrants": "Warrants",
    "options": "Options",
    "rsus": "Unvested RSUs",
    "convertible_shares": "Convertible shares",
}
JOB_MAX_AGE = timedelta(hours=30)
CALIBRATION_MAX_AGE = timedelta(days=8)
TITLE_MAX = 90


def _stale(engine: Engine, job: str, max_age: timedelta) -> tuple[str | None, bool]:
    last = last_ok_finished(engine, job)
    if last is None:
        return None, True
    return last, datetime.now(UTC) - datetime.fromisoformat(last) > max_age


def freshness(engine: Engine) -> dict[str, Any]:
    out = {}
    for job in ("reactions", "scorecards", "theme"):
        last, stale = _stale(engine, job, JOB_MAX_AGE)
        out[job] = {"last_ok": last, "stale": stale}
    last, stale = _stale(engine, "calibration", CALIBRATION_MAX_AGE)
    out["calibration"] = {"last_ok": last, "stale": stale}
    return out


def latest_scorecard(engine: Engine, symbol: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        r = conn.execute(
            select(scorecards)
            .where(scorecards.c.symbol == symbol)
            .order_by(scorecards.c.as_of.desc())
            .limit(1)
        ).first()
    if r is None:
        return None
    payload = json.loads(r.components)
    comps = [
        {"key": k, "label": COMPONENT_LABELS.get(k, k), **v}
        for k, v in payload["components"].items()
    ]
    order = list(COMPONENT_LABELS)
    comps.sort(key=lambda c: order.index(c["key"]) if c["key"] in order else 99)
    fund = payload["components"].get("fundamentals", {}).get("metrics", {}).get("snapshot")
    return {
        "as_of": r.as_of,
        "total": r.total,
        "coverage": r.coverage,
        "components": comps,
        "missing": payload.get("missing", []),
        "fundamentals": fund,
    }


def latest_totals(engine: Engine) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                scorecards.c.symbol, scorecards.c.as_of, scorecards.c.total, scorecards.c.coverage
            ).order_by(scorecards.c.symbol, scorecards.c.as_of)
        ).all()
    return {r.symbol: {"as_of": r.as_of, "total": r.total, "coverage": r.coverage} for r in rows}


def theme_card(engine: Engine) -> dict[str, Any] | None:
    with engine.connect() as conn:
        r = conn.execute(
            select(theme_decomposition).order_by(theme_decomposition.c.as_of.desc()).limit(1)
        ).first()
    if r is None:
        return None
    return {
        "as_of": r.as_of,
        "n": r.n_sessions,
        "betas": json.loads(r.betas),
        "attribution": json.loads(r.attribution),
        "r2": r.r2,
        "partial_r2": r.quantum_partial_r2,
        "weight": r.watchlist_weight_in_qtum,
        "members": json.loads(r.basket_members),
    }


def agreement(
    cls: str, direction: int | None, status: str, car_5: float | None, z_5: float | None
) -> str | None:
    if status != "complete":
        return None
    if cls == "NOISE":
        return None if z_5 is None else ("agreed" if abs(z_5) < 2 else "disagreed")
    if not direction or car_5 is None:
        return None
    return "agreed" if (car_5 > 0) == (direction > 0) else "disagreed"


def _reaction_query(symbol: str) -> Any:
    return (
        select(
            event_reactions,
            events.c.title,
            events.c.url,
            event_classifications.c["class"],
            event_classifications.c.category,
            event_classifications.c.materiality,
            event_classifications.c.direction.label("class_direction"),
            event_tickers.c.direction.label("ticker_direction"),
        )
        .join(events, events.c.id == event_reactions.c.event_id)
        .join(event_classifications, event_classifications.c.event_id == event_reactions.c.event_id)
        .outerjoin(
            event_tickers,
            (event_tickers.c.event_id == event_reactions.c.event_id)
            & (event_tickers.c.symbol == event_reactions.c.symbol),
        )
        .where(event_reactions.c.symbol == symbol, events.c.quarantined == 0)
    )


def _reaction_row(r: Any) -> dict[str, Any]:
    m = dict(r._mapping)
    d = m.pop("ticker_direction")
    cd = m.pop("class_direction")
    m["direction"] = d if d is not None else cd
    m["agreement"] = agreement(m["class"], m["direction"], m["status"], m["car_5"], m["z_5"])
    m["confounders"] = json.loads(m["confounders"])
    return m


def reactions_for(engine: Engine, symbol: str, limit: int = 40) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            _reaction_query(symbol)
            .where(event_reactions.c.t0.is_not(None))
            .order_by(event_reactions.c.t0.desc(), event_reactions.c.event_id.desc())
            .limit(limit)
        ).all()
    return [_reaction_row(r) for r in rows]


def markers(engine: Engine, symbol: str) -> list[dict[str, Any]]:
    """Event markers for the price chart (t0, class, short title, z5). Plain data: the chart
    draws tooltips on the canvas, so no HTML is built from these strings."""
    with engine.connect() as conn:
        rows = conn.execute(
            _reaction_query(symbol)
            .where(event_reactions.c.t0.is_not(None))
            .order_by(event_reactions.c.t0, event_reactions.c.event_id)
        ).all()
    out = []
    for r in rows:
        m = _reaction_row(r)
        title = m["title"] if len(m["title"]) <= TITLE_MAX else m["title"][: TITLE_MAX - 1] + "…"
        out.append(
            {
                "d": m["t0"],
                "cls": m["class"],
                "category": m["category"],
                "materiality": m["materiality"],
                "title": title,
                "z5": m["z_5"],
                "status": m["status"],
                "agreement": m["agreement"],
            }
        )
    return out


def calibration_view(engine: Engine, history: int = 26) -> dict[str, Any] | None:
    with engine.connect() as conn:
        rows = conn.execute(
            select(calibration_reports.c.as_of, calibration_reports.c.payload)
            .order_by(calibration_reports.c.as_of.desc())
            .limit(history)
        ).all()
    if not rows:
        return None
    latest = json.loads(rows[0].payload)
    trend = []
    for r in rows:
        p = json.loads(r.payload)
        trend.append(
            {
                "as_of": r.as_of,
                "usable": p["counts"]["usable"],
                **{
                    c: {
                        "n": p["classes"][c]["n"],
                        "mean_abs_z5": p["classes"][c]["mean_abs_z5"],
                        "hit": p["classes"][c].get("direction_hit_rate"),
                    }
                    for c in ("SIGNAL", "NOISE", "RISK")
                },
            }
        )
    return {"latest": latest, "trend": trend}
