"""Conclusion runs (spec §6.2): context → no-tools call → citation validator (retry once) →
hysteresis → one short write per conclusion.

Order per run: every active pure-play, then QTUM (the quantum-sleeve view), then the theme tilt
(which reads the stances just written). Calls are synchronous under the daily soft budget; a
budget refusal stops the run (the remaining names wait for the next run). An API error or a
second invalid answer stores a `conclusion_failures` row (error text only) and moves on.

Holdings, cash, the account and the mandate never enter a prompt (`synthesize/context.py`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, insert, select

from aether.config import ConclusionParams, LlmConfig, TrackRecordParams
from aether.db.engine import write_tx
from aether.db.models import conclusion_failures, conclusions, events, scorecards, tickers
from aether.db.types import utcnow_iso
from aether.facts import Fact
from aether.llm.client import BudgetExceeded, LlmClient, LlmError
from aether.llm.pricing import cost_usd, usage_from_dict
from aether.portfolio.job import canon
from aether.runs import JobResult
from aether.synthesize import prompt
from aether.synthesize.context import CORE, Context, theme_context, ticker_context
from aether.synthesize.hysteresis import CitedEvent, Decision, Previous, decide
from aether.synthesize.validate import InvalidConclusion, Validated, validate

log = logging.getLogger(__name__)
PURPOSE = "synthesis"


@dataclass(frozen=True)
class SynthDeps:
    llm: LlmClient
    llm_cfg: LlmConfig
    model: str
    conclusions: ConclusionParams
    track: TrackRecordParams
    facts: Sequence[Fact]
    # M13: an escalation's re-synthesis runs as `synthesis_escalation` (escalation sub-budget).
    purpose: str = PURPOSE


def _previous(engine: Engine, kind: str, symbol: str | None) -> Previous | None:
    with engine.connect() as conn:
        q = select(
            conclusions.c.id,
            conclusions.c.stance,
            conclusions.c.created_at,
            conclusions.c.as_of,
            conclusions.c.held,
        )
        q = (
            q.where(conclusions.c.symbol == symbol)
            if kind == "ticker"
            else q.where(conclusions.c.kind == "theme")
        )
        rows = conn.execute(q.order_by(conclusions.c.id)).all()
    if not rows:
        return None
    last_change = rows[0].as_of
    stance = rows[0].stance
    for r in rows[1:]:
        if r.stance != stance:
            last_change, stance = r.as_of, r.stance
    last = rows[-1]
    return Previous(last.id, last.stance, last.created_at, last_change)


def _totals(engine: Engine, symbol: str | None, as_of: date, n: int) -> list[float | None]:
    if symbol is None:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            select(scorecards.c.total)
            .where(scorecards.c.symbol == symbol, scorecards.c.as_of <= as_of.isoformat())
            .order_by(scorecards.c.as_of.desc())
            .limit(n)
        ).scalars()
        return list(reversed(list(rows)))


def _event_states(
    engine: Engine, ids: Sequence[int], known: dict[int, CitedEvent]
) -> dict[int, CitedEvent]:
    """Re-read the cited events right before the write: one quarantined since the context was
    built must not count (and is rejected by the caller)."""
    if not ids:
        return {}
    with engine.connect() as conn:
        q = {
            r.id: bool(r.quarantined)
            for r in conn.execute(
                select(events.c.id, events.c.quarantined).where(events.c.id.in_(sorted(set(ids))))
            )
        }
    return {
        i: CitedEvent(e.id, e.cls, e.materiality, e.trust_tier, e.published_at, q.get(i, True))
        for i, e in known.items()
        if i in q
    }


def _call(deps: SynthDeps, ctx: Context) -> tuple[Validated | None, Decimal, int, str | None]:
    """(validated or None, cost, attempts, last error). BudgetExceeded propagates."""
    pv = prompt.prompt_version()
    cost = Decimal(0)
    err: str | None = None
    attempts = 0
    for _ in range(deps.llm_cfg.synthesis.max_attempts):
        attempts += 1
        try:
            msg = deps.llm.complete(
                purpose=deps.purpose,
                model=deps.model,
                system=prompt.system_prompt(ctx.kind),
                messages=[
                    {"role": "user", "content": prompt.user_message(ctx.text, ctx.kind, err)}
                ],
                max_tokens=deps.llm_cfg.synthesis.max_tokens,
                effort=deps.llm_cfg.synthesis.effort,
                output_format=prompt.output_format(ctx.kind),
            )
        except BudgetExceeded:
            raise
        except LlmError as exc:
            return None, cost, attempts, f"API error: {exc}"[:500]
        cost += cost_usd(deps.llm_cfg, deps.model, usage_from_dict(msg.get("usage")))
        try:
            return validate(msg, ctx), cost, attempts, None
        except InvalidConclusion as exc:
            err = str(exc)[:300]
            log.warning("synthesis %s %s rejected: %s (prompt %s)", ctx.kind, ctx.symbol, err, pv)
    return None, cost, attempts, err


def _fail(engine: Engine, ctx: Context, model: str, attempts: int, error: str) -> None:
    with write_tx(engine) as conn:
        conn.execute(
            insert(conclusion_failures).values(
                kind=ctx.kind,
                symbol=ctx.symbol,
                as_of=ctx.as_of,
                attempts=attempts,
                error=error[:1000],
                model=model,
                prompt_version=prompt.prompt_version(),
                created_at=utcnow_iso(),
            )
        )


def synthesize_one(
    engine: Engine, deps: SynthDeps, kind: str, symbol: str | None, as_of: date
) -> dict[str, Any]:
    """One conclusion. Returns a small summary dict (never model text)."""
    if kind == "theme":
        ctx = theme_context(engine, as_of, deps.conclusions, deps.track, deps.facts)
    else:
        assert symbol is not None
        ctx = ticker_context(engine, symbol, as_of, deps.conclusions, deps.track, deps.facts)
    v, cost, attempts, err = _call(deps, ctx)
    if v is None:
        _fail(engine, ctx, deps.model, attempts, err or "invalid")
        return {"symbol": symbol or "theme", "status": "failed", "error": err}

    cited_events = v.cited_event_ids()
    states = _event_states(engine, cited_events, ctx.events)
    if any(states.get(i) is None or states[i].quarantined for i in cited_events):
        _fail(engine, ctx, deps.model, attempts, "a cited event was quarantined before the write")
        return {"symbol": symbol or "theme", "status": "failed", "error": "quarantined citation"}

    prev = _previous(engine, kind, symbol)
    decision: Decision = decide(
        kind=kind,
        proposed=v.stance,
        previous=prev,
        cited_event_ids=v.cited_event_ids(v.justification_ids),
        events=states,
        totals=_totals(engine, symbol, as_of, deps.conclusions.consecutive_days),
        as_of=as_of,
        p=deps.conclusions,
    )
    payload = {
        **v.payload,
        "hysteresis": {
            "proposed": v.stance,
            "previous": prev.stance if prev else None,
            "held": decision.held,
            "reason": decision.reason,
            "trigger": decision.trigger,
        },
    }
    pv = prompt.prompt_version()
    with write_tx(engine) as conn:
        new_id = conn.execute(
            insert(conclusions)
            .values(
                kind=kind,
                symbol=symbol,
                as_of=ctx.as_of,
                created_at=utcnow_iso(),
                stance=decision.stance,
                proposed_stance=v.stance,
                held=int(decision.held),
                hold_reason=decision.reason if decision.held else None,
                confidence=v.confidence,
                horizon=v.horizon,
                payload=canon(payload),
                evidence=canon({k: ctx.evidence[k] for k in v.cited}),
                model=deps.model,
                prompt_version=pv,
                input_hash=ctx.digest(deps.model, pv),
                cost_micros=cost,
                prev_id=prev.id if prev else None,
            )
            .returning(conclusions.c.id)
        ).scalar_one()
    return {
        "symbol": symbol or "theme",
        "status": "held" if decision.held else "ok",
        "id": new_id,
        "stance": decision.stance,
    }


def synth_symbols(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        pure = list(
            conn.execute(
                select(tickers.c.symbol)
                .where(tickers.c.type == "pure_play", tickers.c.active == 1)
                .order_by(tickers.c.symbol)
            ).scalars()
        )
        core = conn.execute(select(tickers.c.symbol).where(tickers.c.symbol == CORE)).first()
    return pure + ([CORE] if core else [])


def run_conclusions(
    engine: Engine,
    deps: SynthDeps,
    as_of: date,
    symbols: Sequence[str] | None = None,
    include_theme: bool = True,
) -> JobResult:
    targets: list[tuple[str, str | None]] = [
        ("ticker", s) for s in (symbols if symbols is not None else synth_symbols(engine))
    ]
    if include_theme:
        targets.append(("theme", None))
    written = 0
    failed: list[str] = []
    held: list[str] = []
    for kind, sym in targets:
        try:
            out = synthesize_one(engine, deps, kind, sym, as_of)
        except BudgetExceeded as exc:
            left = len(targets) - written - len(failed)
            return JobResult(
                rows_written=written,
                warning=f"daily LLM budget reached; {left} conclusions wait: {exc}"[:500],
            )
        if out["status"] == "failed":
            failed.append(out["symbol"])
        else:
            written += 1
            if out["status"] == "held":
                held.append(out["symbol"])
    notes = []
    if failed:
        notes.append("failed validation: " + ", ".join(failed))
    if held:
        notes.append("held by hysteresis: " + ", ".join(held))
    return JobResult(rows_written=written, warning="; ".join(notes) or None)
