"""Weekly brief (spec §6.2; Sunday 09:00 SGT). Deterministic: built from the database, no LLM.

Sections: what changed (stance changes, held proposals, score moves), the top 5 signals, risks,
filtered noise (counts), upcoming catalysts and earnings, the stance table with each stance's
track record, a one-line event-reaction note, and position drift (if holdings are saved).

Telegram (owner decision 2026-10-06): plain text, ≤ 4096 chars, sent once per ISO week (dedupe
key `weekly_brief:YYYY-Www`). Holdings never leave the machine: the Telegram text has only the
number of names outside the no-trade band, never weights, share counts or dollar values. Event
titles are shown as stored (untrusted text, sent as plain text without link previews).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, select

from aether.alerts.candidates import AlertCandidate
from aether.alerts.dispatch import enqueue
from aether.config import RiskFlagParams, ShortInterestRule, TrackRecordParams
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    briefs,
    calibration_reports,
    conclusions,
    earnings_calendar,
    event_classifications,
    event_tickers,
    events,
    rebalance_plans,
    scorecards,
)
from aether.db.types import utcnow_iso
from aether.portfolio.holdings import load_holdings, load_settings, universe_symbols
from aether.portfolio.job import canon
from aether.portfolio.rebalance import drift_lines
from aether.review.pack import fit, upcoming_catalysts
from aether.risk.flags import open_flags
from aether.runs import JobResult
from aether.synthesize.view import DISCLAIMER, stance_line, stance_table

WINDOW_DAYS = 7
UPCOMING_DAYS = 30
TITLE_MAX = 120


def week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _clip(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= TITLE_MAX else s[: TITLE_MAX - 1] + "…"


def _events(engine: Engine, cls: str, since: str, limit: int) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                events.c.id,
                events.c.title,
                events.c.published_at,
                events.c.trust_tier,
                event_classifications.c.category,
                event_classifications.c.materiality,
                event_classifications.c.direction,
                event_classifications.c.confidence,
            )
            .join(event_classifications, event_classifications.c.event_id == events.c.id)
            .where(
                event_classifications.c["class"] == cls,
                events.c.quarantined == 0,
                events.c.published_at >= since,
            )
            .order_by(
                event_classifications.c.materiality.desc(),
                event_classifications.c.confidence.desc(),
                events.c.published_at.desc(),
                events.c.id,
            )
            .limit(limit)
        ).all()
        out = []
        for r in rows:
            syms = list(
                conn.execute(
                    select(event_tickers.c.symbol)
                    .where(event_tickers.c.event_id == r.id)
                    .order_by(event_tickers.c.symbol)
                ).scalars()
            )
            out.append({**dict(r._mapping), "title": _clip(r.title), "symbols": syms})
    return out


def _counts(engine: Engine, since: str) -> dict[str, int]:
    with engine.connect() as conn:
        by_class: dict[str, int] = dict(
            conn.execute(
                select(event_classifications.c["class"], func.count())
                .join(events, events.c.id == event_classifications.c.event_id)
                .where(events.c.published_at >= since, events.c.quarantined == 0)
                .group_by(event_classifications.c["class"])
            ).all()
        )
        quarantined = conn.execute(
            select(func.count()).where(events.c.published_at >= since, events.c.quarantined == 1)
        ).scalar_one()
    return {
        "SIGNAL": int(by_class.get("SIGNAL", 0)),
        "NOISE": int(by_class.get("NOISE", 0)),
        "RISK": int(by_class.get("RISK", 0)),
        "quarantined": int(quarantined),
    }


def _score_moves(engine: Engine, today: date) -> list[dict[str, Any]]:
    week_ago = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            select(scorecards.c.symbol, scorecards.c.as_of, scorecards.c.total).order_by(
                scorecards.c.symbol, scorecards.c.as_of
            )
        ).all()
    latest: dict[str, Any] = {}
    before: dict[str, Any] = {}
    for r in rows:
        latest[r.symbol] = r
        if r.as_of <= week_ago:
            before[r.symbol] = r
    out = []
    for s, r in sorted(latest.items()):
        b = before.get(s)
        if r.total is None or b is None or b.total is None:
            continue
        out.append({"symbol": s, "total": r.total, "prev": b.total, "delta": r.total - b.total})
    return out


def _changes(engine: Engine, since: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                conclusions.c.id,
                conclusions.c.kind,
                conclusions.c.symbol,
                conclusions.c.stance,
                conclusions.c.proposed_stance,
                conclusions.c.held,
                conclusions.c.hold_reason,
                conclusions.c.prev_id,
                conclusions.c.as_of,
            )
            .where(conclusions.c.as_of >= since)
            .order_by(conclusions.c.id)
        ).all()
        prev = {
            r.id: r.stance
            for r in conn.execute(
                select(conclusions.c.id, conclusions.c.stance).where(
                    conclusions.c.id.in_([r.prev_id for r in rows if r.prev_id])
                )
            )
        }
    out = []
    for r in rows:
        before = prev.get(r.prev_id) if r.prev_id else None
        who = r.symbol or "Theme tilt"
        if r.held:
            out.append(
                {
                    "who": who,
                    "id": r.id,
                    "text": f"proposed {r.proposed_stance}, held at {r.stance} ({r.hold_reason})",
                }
            )
        elif before is None:
            out.append({"who": who, "id": r.id, "text": f"first conclusion: {r.stance}"})
        elif before != r.stance:
            out.append({"who": who, "id": r.id, "text": f"{before} → {r.stance}"})
    return out


def _reaction_note(engine: Engine) -> str:
    with engine.connect() as conn:
        r = conn.execute(
            select(calibration_reports).order_by(calibration_reports.c.as_of.desc()).limit(1)
        ).first()
    if r is None:
        return "Event reactions: no calibration report yet."
    rep = json.loads(r.payload)
    reported = [c for c in rep.get("categories", []) if not c.get("too_few")]
    usable = rep.get("counts", {}).get("usable", 0)
    return (
        f"Event reactions (calibration {r.as_of}): {usable} usable reactions, "
        f"{len(reported)} categories with enough events, {len(rep.get('flags', []))} flags, "
        f"{rep.get('spot_review_total', 0)} items for spot review."
    )


def _drift(engine: Engine) -> dict[str, Any] | None:
    if load_holdings(engine).empty:
        return None
    profile = load_settings(engine).selected_profile
    with engine.connect() as conn:
        row = conn.execute(
            select(rebalance_plans.c.plan, rebalance_plans.c.as_of)
            .where(rebalance_plans.c.profile == profile)
            .order_by(rebalance_plans.c.as_of.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    plan = json.loads(row.plan)
    return {
        "profile": profile,
        "targets_as_of": row.as_of,
        "lines": drift_lines(plan),
        "trades": len(plan.get("trades", [])),
    }


def build_brief(
    engine: Engine,
    today: date,
    track: TrackRecordParams,
    flags_params: RiskFlagParams,
    short_rule: ShortInterestRule | None = None,
) -> dict[str, Any]:
    since_day = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    since = f"{since_day}T00:00:00Z"
    universe = universe_symbols(engine)
    pure = [s for s in universe if s != "QTUM"]
    with engine.connect() as conn:
        earnings = [
            {"symbol": s, "date": d}
            for s, d in conn.execute(
                select(earnings_calendar.c.symbol, earnings_calendar.c.date)
                .where(
                    earnings_calendar.c.symbol.in_(universe),
                    earnings_calendar.c.date >= today.isoformat(),
                    earnings_calendar.c.date <= (today + timedelta(days=UPCOMING_DAYS)).isoformat(),
                )
                .order_by(earnings_calendar.c.date, earnings_calendar.c.symbol)
            )
        ]
    stances = stance_table(engine, track, today)
    return {
        "as_of": today.isoformat(),
        "week": week_key(today),
        "since": since_day,
        "changes": _changes(engine, since_day),
        "score_moves": _score_moves(engine, today),
        "signals": _events(engine, "SIGNAL", since, 5),
        "risks": _events(engine, "RISK", since, 10),
        "flags": [
            {"symbol": f.symbol, "kind": f.kind, "detail": f.detail}
            for f in open_flags(engine, flags_params, today, pure, short_rule)
        ],
        "counts": _counts(engine, since),
        "catalysts": upcoming_catalysts(engine, today, UPCOMING_DAYS),
        "earnings": earnings,
        "stances": [
            {
                **{k: v for k, v in s.items() if k != "standing"},
                "standing": asdict(s["standing"]),
                "line": stance_line(s),
            }
            for s in stances
        ],
        "reaction_note": _reaction_note(engine),
        "drift": _drift(engine),
    }


def telegram_text(b: dict[str, Any]) -> str:
    lines = [f"Aether weekly brief · {b['as_of']} ({b['week']})", ""]
    if b["changes"]:
        lines.append("What changed:")
        lines += [f"- {c['who']}: {c['text']}" for c in b["changes"]]
    else:
        lines.append("What changed: no stance changes.")
    big = [m for m in b["score_moves"] if abs(m["delta"]) >= 5]
    if big:
        lines.append(
            "Score moves ≥ 5 points: "
            + ", ".join(f"{m['symbol']} {m['prev']:+.0f} → {m['total']:+.0f}" for m in big)
        )
    lines += ["", "Stances (track record):"]
    lines += [s["line"] for s in b["stances"]] or ["- no conclusions yet"]
    if b["signals"]:
        lines += ["", "Top signals:"]
        lines += [
            f"- #{e['id']} {'/'.join(e['symbols']) or 'theme'} m{e['materiality']} "
            f"{e['category']}: {e['title']}"
            for e in b["signals"]
        ]
    if b["risks"] or b["flags"]:
        lines += ["", "Risks:"]
        lines += [
            f"- #{e['id']} {'/'.join(e['symbols']) or 'theme'} m{e['materiality']} "
            f"{e['category']}: {e['title']}"
            for e in b["risks"]
        ]
        lines += [f"- open flag {f['symbol']}: {f['detail']}" for f in b["flags"]]
    c = b["counts"]
    lines += [
        "",
        f"Filtered noise: {c['NOISE']} NOISE items, {c['quarantined']} quarantined "
        f"(signals {c['SIGNAL']}, risks {c['RISK']} this week).",
    ]
    if b["catalysts"] or b["earnings"]:
        lines += ["", f"Next {UPCOMING_DAYS} days:"]
        lines += [f"- {e['date']} {e['symbol']} earnings" for e in b["earnings"]]
        for cat in b["catalysts"]:
            label = "" if cat["fact_status"] in (None, "signed_off") else " [unconfirmed]"
            who = f"{cat['symbol']}: " if cat.get("symbol") else ""
            lines.append(f"- {cat['window_start']} {who}{cat['title']}{label}")
    lines += ["", b["reaction_note"]]
    d = b["drift"]
    if d is not None:
        lines.append(
            f"Position drift: {d['trades']} names outside the no-trade band (see /holdings)."
        )
    lines += ["", f"Full brief: /briefs. {DISCLAIMER}"]
    return fit(lines, where="/briefs")


def weekly_brief(
    engine: Engine,
    track: TrackRecordParams,
    flags_params: RiskFlagParams,
    *,
    today: date,
    telegram: bool,
    short_rule: ShortInterestRule | None = None,
    now: datetime | None = None,
) -> JobResult:
    week = week_key(today)
    with engine.connect() as conn:
        done = conn.execute(
            select(briefs.c.as_of).where(briefs.c.week == week, briefs.c.status == "done")
        ).first()
    if done is not None:
        return JobResult(warning=f"brief for {week} already done")
    try:
        b = build_brief(engine, today, track, flags_params, short_rule)
        text = telegram_text(b)
    except Exception as exc:
        with write_tx(engine) as conn:
            upsert(
                conn,
                briefs,
                [
                    {
                        "as_of": today.isoformat(),
                        "week": week,
                        "payload": "{}",
                        "telegram_text": None,
                        "status": "failed",
                        "error": repr(exc)[:500],
                        "created_at": utcnow_iso(),
                    }
                ],
                key_cols=["week"],
            )
        raise
    with write_tx(engine) as conn:
        upsert(
            conn,
            briefs,
            [
                {
                    "as_of": today.isoformat(),
                    "week": week,
                    "payload": canon(b),
                    "telegram_text": text,
                    "status": "done",
                    "error": None,
                    "created_at": utcnow_iso(),
                }
            ],
            key_cols=["week"],
        )
    enqueue(
        engine,
        [AlertCandidate(kind="weekly_brief", dedupe_key=f"weekly_brief:{week}", text=text)],
        telegram=telegram,
        now=now or datetime.now(UTC),
    )
    return JobResult(rows_written=1)
