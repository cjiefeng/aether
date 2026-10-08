"""Monthly review pack (spec §6.9, M5 sections): the owner's decision document for the month.

On the 1st at 10:30 SGT (retried once on the 2nd if it failed): publish targets (§6.6), refresh
the plan, then build one pack and queue one Telegram message (dedupe key `review_pack:YYYY-MM`).

M5 sections: the selected profile's published targets with each adjustment chain, the rebalance
plan, drift, value in USD and SGD, open risk flags, upcoming earnings and lock-ups. M8 adds the
upcoming catalysts and the options panel per name (research only). M10 adds the stances with
their track record and the overlay's value-added line (layer 3). M12 adds the month's universe
review proposals (add / remove / watch), or its status if it isn't done.

Holdings never leave the machine (§1.3): the Telegram text carries target weights, flags, dates
and the number of suggested trades only. No share counts, dollar values or account number.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select

from aether.alerts.candidates import AlertCandidate
from aether.alerts.dispatch import enqueue
from aether.config import RiskFlagParams, ShortInterestRule, StrategiesConfig, WeightsConfig
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    catalysts,
    earnings_calendar,
    facts,
    fx_rates,
    lockups,
    rebalance_plans,
    review_packs,
)
from aether.db.types import utcnow_iso
from aether.options.view import options_panel
from aether.portfolio.holdings import load_holdings, load_settings, universe_symbols
from aether.portfolio.job import canon
from aether.portfolio.overlay import chain_text
from aether.portfolio.publish import latest_published, publish_targets
from aether.portfolio.rebalance import drift_summary
from aether.risk.flags import open_flags
from aether.runs import JobResult

MAX_TELEGRAM = 4096
UPCOMING_DAYS = 45
LOCKUP_DAYS = 90
CATALYST_DAYS = 90


def upcoming_catalysts(
    engine: Engine, today: date, days: int = CATALYST_DAYS
) -> list[dict[str, Any]]:
    """Upcoming catalysts whose window starts within `days` (or has started and not ended)."""
    t = today.isoformat()
    horizon = (today + timedelta(days=days)).isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                catalysts.c.id,
                catalysts.c.symbol,
                catalysts.c.title,
                catalysts.c.kind,
                catalysts.c.window_start,
                catalysts.c.window_end,
                catalysts.c.fact_id,
                facts.c.status.label("fact_status"),
            )
            .outerjoin(facts, facts.c.id == catalysts.c.fact_id)
            .where(
                catalysts.c.status == "upcoming",
                catalysts.c.window_start <= horizon,
                (catalysts.c.window_end.is_(None)) | (catalysts.c.window_end >= t),
            )
            .order_by(catalysts.c.window_start, catalysts.c.symbol, catalysts.c.id)
        ).all()
    return [dict(r._mapping) for r in rows]


def latest_fx(engine: Engine) -> tuple[str, float] | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(fx_rates.c.d, fx_rates.c.rate)
            .where(fx_rates.c.pair == "USDSGD")
            .order_by(fx_rates.c.d.desc())
            .limit(1)
        ).first()
    return None if row is None else (row.d, row.rate)


def _upcoming(engine: Engine, symbols: list[str], today: date) -> dict[str, list[dict[str, str]]]:
    t = today.isoformat()
    with engine.connect() as conn:
        earnings = [
            {"symbol": s, "date": d}
            for s, d in conn.execute(
                select(earnings_calendar.c.symbol, earnings_calendar.c.date)
                .where(
                    earnings_calendar.c.symbol.in_(symbols),
                    earnings_calendar.c.date >= t,
                    earnings_calendar.c.date <= (today + timedelta(days=UPCOMING_DAYS)).isoformat(),
                )
                .order_by(earnings_calendar.c.date, earnings_calendar.c.symbol)
            )
        ]
        locks = [
            {"symbol": s, "date": d}
            for s, d in conn.execute(
                select(lockups.c.symbol, lockups.c.expiry_date)
                .where(
                    lockups.c.symbol.in_(symbols),
                    lockups.c.expiry_date >= t,
                    lockups.c.expiry_date <= (today + timedelta(days=LOCKUP_DAYS)).isoformat(),
                )
                .order_by(lockups.c.expiry_date, lockups.c.symbol)
            )
        ]
    return {"earnings": earnings, "lockups": locks}


def _m10(engine: Engine, weights: WeightsConfig, profile: str, today: date) -> dict[str, Any]:
    """Stances with their track record, and the overlay's value-added line (M10)."""
    from aether.score.track_record import overlay_value
    from aether.synthesize.view import stance_line, stance_table

    rows = stance_table(engine, weights.track_record, today)
    with engine.connect() as conn:
        ov = overlay_value(conn).get(profile)
    return {
        "stances": [
            {
                "key": r["key"],
                "stance": r["stance"],
                "proposed": r["proposed"],
                "held": r["held"],
                "confidence": r["confidence"],
                "as_of": r["as_of"],
                "id": r["id"],
                "track": r["standing"].label,
                "line": stance_line(r),
            }
            for r in rows
        ],
        "overlay_value": (ov or {}).get("verdict") or "no publish has a 1-month result yet.",
    }


def _m12(engine: Engine, today: date) -> dict[str, Any]:
    """This month's universe review proposals (M12, spec §6.9)."""
    from aether.universe.view import pack_section

    return pack_section(engine, today.strftime("%Y-%m"))


def universe_lines(u: dict[str, Any] | None) -> list[str]:
    if not u:
        return []
    lines = ["", "Universe review (pure-plays):"]
    status = u.get("status")
    if status == "none":
        return [*lines, "- not run yet this month (see /universe)"]
    if status != "done":
        return [*lines, f"- {status} (see /universe)"]
    props = u.get("proposals") or []
    changes = [p for p in props if p["action"] in ("add", "remove")]
    if not changes:
        lines.append("- No changes proposed.")
    for p in props:
        lines.append(
            f"- {p['action']} {p['symbol']}" + (f" ({p['name']})" if p.get("name") else "")
        )
    return lines


def build_pack(
    engine: Engine,
    flags_params: RiskFlagParams,
    today: date,
    short_rule: ShortInterestRule | None = None,
    weights: WeightsConfig | None = None,
) -> dict[str, Any]:
    settings = load_settings(engine)
    profile = settings.selected_profile
    with engine.connect() as conn:
        target = latest_published(conn, profile)
        plan_row = conn.execute(
            select(rebalance_plans.c.plan)
            .where(rebalance_plans.c.profile == profile)
            .order_by(rebalance_plans.c.as_of.desc())
            .limit(1)
        ).first()
    if target is None:
        raise RuntimeError(f"no published targets for {profile}")
    plan = json.loads(plan_row.plan) if plan_row else None
    universe = universe_symbols(engine)
    pure = [s for s in universe if s != "QTUM"]
    fx = latest_fx(engine)
    value: dict[str, Any] | None = None
    if plan is not None and not load_holdings(engine).empty:
        usd = float(plan["total_value"])
        value = {
            "usd": plan["total_value"],
            "sgd": None if fx is None else f"{usd * fx[1]:.2f}",
            "fx_rate": None if fx is None else fx[1],
            "fx_date": None if fx is None else fx[0],
        }
    flags = [
        {"symbol": f.symbol, "kind": f.kind, "detail": f.detail, "as_of": f.as_of}
        for f in open_flags(engine, flags_params, today, pure, short_rule)
    ]
    return {
        "as_of": today.isoformat(),
        "profile": profile,
        "targets": {
            "as_of": target.as_of,
            "published_at": target.published_at,
            "trigger": target.trigger,
            "strategy_id": target.strategy_id,
            "prices_as_of": target.prices_as_of,
            "weights": target.weights,
            "chain": target.adjustments.get("chain", []),
            "note": target.adjustments.get("note"),
        },
        "plan": plan,
        "drift": drift_summary(plan) if plan else [],
        "value": value,
        "flags": flags,
        "upcoming": _upcoming(engine, universe, today),
        "catalysts": upcoming_catalysts(engine, today),
        "options": options_panel(engine, universe),
        **(_m10(engine, weights, profile, today) if weights is not None else {}),
        "universe": _m12(engine, today),
    }


def telegram_text(pack: dict[str, Any]) -> str:
    """Plain text, ≤4096 chars. Target weights, flags, dates and the number of suggested trades;
    never share counts, dollar values or the account number."""
    t = pack["targets"]
    lines = [
        f"Aether monthly review · {pack['as_of']}",
        f"Profile: {pack['profile']}. Targets published {t['as_of']} "
        f"(prices to {t['prices_as_of']}).",
        "",
        "Targets:",
    ]
    chains = {c["symbol"]: c for c in t["chain"]}
    for sym, w in sorted(t["weights"].items(), key=lambda x: (-x[1], x[0])):
        c = chains.get(sym)
        extra = f" ({chain_text(c)})" if c and (c["steps"] or c["redistributed"]) else ""
        lines.append(f"- {sym} {w * 100:.1f}%{extra}")
    for sym, c in sorted(chains.items()):
        if sym not in t["weights"] and c["steps"]:
            lines.append(f"- {sym} 0.0% ({chain_text(c)})")
    if t.get("note"):
        lines.append(t["note"])
    lines.append("")
    if pack["plan"] is None:
        lines.append("No holdings saved: no rebalance plan.")
    else:
        n = len(pack["plan"]["trades"])
        lines.append(f"Suggested trades: {n}" + (" (see Holdings)." if n else "."))
    if pack["flags"]:
        lines += ["", "Open risk flags:"]
        lines += [f"- {f['symbol']}: {f['detail']}" for f in pack["flags"]]
    up = pack["upcoming"]
    if up["earnings"] or up["lockups"]:
        lines += ["", "Upcoming:"]
        lines += [f"- {e['date']} {e['symbol']} earnings" for e in up["earnings"]]
        lines += [f"- {e['date']} {e['symbol']} lock-up ends" for e in up["lockups"]]
    cats = pack.get("catalysts") or []
    if cats:
        lines += ["", "Catalysts (next 90 days):"]
        for c in cats:
            when = c["window_start"]
            if c["window_end"] != c["window_start"]:
                when += f" to {c['window_end']}" if c["window_end"] else " onwards"
            label = "" if c["fact_status"] in (None, "signed_off") else " [unconfirmed]"
            sym = c.get("symbol")
            who = f"{sym}: " if sym and not c["title"].startswith(sym) else ""
            lines.append(f"- {when} {who}{c['title']}{label}")
    if pack.get("stances") is not None:
        lines += ["", "Stances (track record):"]
        lines += [s["line"] for s in pack["stances"]] or ["- no conclusions yet"]
        lines.append(f"Overlay value-added: {pack['overlay_value']}")
    lines += universe_lines(pack.get("universe"))
    opts = pack.get("options") or []
    if opts:
        lines += ["", "Options (research only, never trades):"]
        lines += [options_line(o) for o in opts]
    lines += ["", "Full pack: /review. Not financial advice."]
    return fit(lines)


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def options_line(o: dict[str, Any]) -> str:
    """One plain line per name: IV30, IV rank, the next implied move. Percentages only."""
    sym = o["symbol"]
    if o.get("d") is None:
        return f"- {sym}: no snapshot yet"
    if o["thin"]:
        return f"- {sym}: thin chain, not reported"
    rank = (
        f"rank {_pct(o['iv_rank'])}"
        if o.get("iv_rank") is not None
        else f"rank: building history ({o.get('iv_history_days') or 0} days)"
    )
    parts = [f"- {sym}: IV30 {_pct(o['atm_iv_30'])}, {rank}"]
    move = next((m for m in o.get("implied_moves", []) if m.get("move") is not None), None)
    if move:
        parts.append(
            f"implied move ±{move['move'] * 100:.1f}% into {move['date']} ({move['label']})"
        )
    return "; ".join(parts)


def fit(lines: list[str], limit: int = MAX_TELEGRAM, where: str = "/review") -> str:
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    kept: list[str] = []
    for i, line in enumerate(lines):
        more = len(lines) - i
        tail = f"… {more} more lines on {where}"
        if len("\n".join([*kept, line, tail])) > limit:
            return "\n".join([*kept, tail])
        kept.append(line)
    return "\n".join(kept)


def month_done(engine: Engine, month: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(review_packs.c.as_of).where(
                    review_packs.c.month == month, review_packs.c.status == "done"
                )
            ).first()
            is not None
        )


def monthly_review(
    engine: Engine,
    config: StrategiesConfig,
    flags_params: RiskFlagParams,
    *,
    today: date,
    short_rule: ShortInterestRule | None = None,
    telegram: bool,
    now: datetime | None = None,
    weights: WeightsConfig | None = None,
) -> JobResult:
    """Publish targets, build the pack, queue the Telegram message. Once per month."""
    month = today.strftime("%Y-%m")
    if month_done(engine, month):
        return JobResult(warning=f"review pack for {month} already done")
    try:
        published = publish_targets(engine, config, today=today, trigger="monthly", weights=weights)
        if published.warning and published.rows_written == 0:
            raise RuntimeError(published.warning)
        pack = build_pack(engine, flags_params, today, short_rule, weights)
        text = telegram_text(pack)
    except Exception as exc:
        with write_tx(engine) as conn:
            upsert(
                conn,
                review_packs,
                [
                    {
                        "as_of": today.isoformat(),
                        "month": month,
                        "payload": "{}",
                        "telegram_text": None,
                        "status": "failed",
                        "error": repr(exc)[:500],
                        "created_at": utcnow_iso(),
                    }
                ],
                key_cols=["as_of"],
            )
        raise
    with write_tx(engine) as conn:
        upsert(
            conn,
            review_packs,
            [
                {
                    "as_of": today.isoformat(),
                    "month": month,
                    "payload": canon(pack),
                    "telegram_text": text,
                    "status": "done",
                    "error": None,
                    "created_at": utcnow_iso(),
                }
            ],
            key_cols=["as_of"],
        )
    enqueue(
        engine,
        [AlertCandidate(kind="review_pack", dedupe_key=f"review_pack:{month}", text=text)],
        telegram=telegram,
        now=now or datetime.now(UTC),
    )
    return JobResult(rows_written=published.rows_written + 1)
