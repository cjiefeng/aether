"""The escalation pass (spec §5.2.5, M11). For each new candidate, in event order:

1. **Claim:** one short `write_tx` inserts the row, `running` or `refused` (with the cap that
   refused it). A running escalation enqueues and delivers an `escalation` alert at once.
2. **Verify:** one research run (web search) for other reports of the same matter. Its results go
   through ingestion like any other source, then the classifier runs on them.
3. **Re-synthesize** that ticker. Hysteresis and the cooldown apply unchanged.
4. **Finish:** one `write_tx` records the outcome; an `escalation_result` alert follows.

The LLM steps run outside any transaction; each write is its own short `write_tx`. A failed or
budget-refused verification still re-synthesizes; the escalation is `failed` only when the
re-synthesis didn't produce a conclusion. The steps are injected so the scheduler can hold its
locks around them and tests can fake them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, insert, select, update

from aether.alerts.candidates import AlertCandidate
from aether.config import EscalationParams
from aether.db.engine import write_tx
from aether.db.models import conclusions, escalations
from aether.db.types import to_iso
from aether.escalate.select import (
    Candidate,
    Recent,
    candidates,
    recent,
    refusal,
)
from aether.llm.pricing import sgt_day_start
from aether.research.runner import VerifyTarget
from aether.runs import JobResult

log = logging.getLogger(__name__)

TITLE_MAX = 300


@dataclass(frozen=True)
class EscalationDeps:
    params: EscalationParams
    max_per_day: int
    lookback_days: int
    symbols: Collection[str]  # tickers that get conclusions
    names: Mapping[str, str]  # symbol → company name, for the research prompt
    # Enqueue (and, with Telegram on, deliver) alerts.
    notify: Callable[[Sequence[AlertCandidate]], None]
    # One verification run → (research_run_id, new events); None when the LLM is off.
    verify: Callable[[VerifyTarget], tuple[int, int]] | None
    # Classify whatever is pending (the new research items).
    classify: Callable[[], None]
    # Re-synthesize one ticker → `synthesize_one`'s summary; None when the LLM is off.
    resynthesize: Callable[[str], dict[str, Any]] | None
    disabled_reason: str | None = None


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def start_alert(c: Candidate, n: int, max_per_day: int) -> AlertCandidate:
    lines = [
        f"Escalated ({n}/{max_per_day} today) · {c.symbol} · {c.cls} "
        f"{c.category.replace('_', ' ')} (materiality {c.materiality}/5)",
        _clip(c.title, TITLE_MAX),
        f"Verification research and a re-synthesis of {c.symbol} follow.",
        f"Published {c.published_at[:10]} · source tier {c.trust_tier} · event #{c.event_id}",
        c.url,
    ]
    return AlertCandidate(
        kind="escalation",
        dedupe_key=f"escalation:{c.event_id}:{c.symbol}",
        text="\n".join(lines),
        payload={"symbol": c.symbol, "trigger": c.trigger, "materiality": c.materiality},
        event_id=c.event_id,
    )


def result_alert(c: Candidate, detail: Mapping[str, Any], conclusion: Any | None) -> AlertCandidate:
    lines = [f"Escalation result · {c.symbol} · event #{c.event_id}"]
    v = detail.get("verify", {})
    if v.get("status") == "done":
        lines.append(
            f"Verification: {v.get('events_new', 0)} new report(s) "
            f"(research run #{v.get('research_run_id')})."
        )
    else:
        lines.append(f"Verification {v.get('status', 'skipped')}: {v.get('error') or '—'}")
    if conclusion is None:
        s = detail.get("synthesis", {})
        lines.append(
            f"Re-synthesis {s.get('status', 'skipped')}: {s.get('error') or '—'}. "
            "The current conclusion stands."
        )
    elif conclusion.held:
        lines.append(
            f"Stance {conclusion.stance} (proposed {conclusion.proposed_stance}, held: "
            f"{_clip(conclusion.hold_reason or 'hysteresis', 300)}) · conclusion "
            f"#{conclusion.id}"
        )
    else:
        lines.append(
            f"Stance {conclusion.stance} (confidence {conclusion.confidence:.2f}) · conclusion "
            f"#{conclusion.id}"
        )
    return AlertCandidate(
        kind="escalation_result",
        dedupe_key=f"escalation_result:{c.event_id}:{c.symbol}",
        text="\n".join(lines),
        payload={"symbol": c.symbol, "conclusion_id": conclusion.id if conclusion else None},
        event_id=c.event_id,
    )


def _claim(engine: Engine, c: Candidate, refused: str | None, now: datetime) -> int:
    with write_tx(engine) as conn:
        return int(
            conn.execute(
                insert(escalations)
                .values(
                    event_id=c.event_id,
                    symbol=c.symbol,
                    trigger=c.trigger,
                    status="refused" if refused else "running",
                    refusal=refused,
                    created_at=to_iso(now),
                    finished_at=to_iso(now) if refused else None,
                )
                .returning(escalations.c.id)
            ).scalar_one()
        )


def _stamp() -> str:
    return to_iso(datetime.now(UTC))


def escalate_one(engine: Engine, deps: EscalationDeps, c: Candidate, esc_id: int) -> str:
    """Steps 2-4 for a claimed escalation. Returns the final status."""
    detail: dict[str, Any] = {"claimed_at": _stamp()}
    research_run_id: int | None = None

    # 2. Verification research, then classify its new items.
    if deps.verify is None:
        detail["verify"] = {"status": "skipped", "error": deps.disabled_reason}
    else:
        target = VerifyTarget(
            event_id=c.event_id,
            symbol=c.symbol,
            name=deps.names.get(c.symbol, c.symbol),
            title=c.title,
            source_domain=c.source_domain,
            published=date.fromisoformat(c.published_at[:10]),
        )
        try:
            research_run_id, new = deps.verify(target)
            detail["verify"] = {
                "status": "done",
                "research_run_id": research_run_id,
                "events_new": new,
            }
        except Exception as exc:  # BudgetExceeded / LlmError: recorded on the research run
            log.warning("escalation %s: verification failed: %s", esc_id, exc)
            detail["verify"] = {"status": "failed", "error": str(exc)[:300]}
        detail["verified_at"] = _stamp()
        if detail["verify"]["status"] == "done" and detail["verify"]["events_new"]:
            try:
                deps.classify()
            except Exception as exc:
                log.warning("escalation %s: classify failed: %s", esc_id, exc)
                detail["classify_error"] = str(exc)[:300]

    # 3. Re-synthesis of the ticker.
    conclusion_id: int | None = None
    if deps.resynthesize is None:
        detail["synthesis"] = {"status": "skipped", "error": deps.disabled_reason}
    else:
        try:
            out = deps.resynthesize(c.symbol)
        except Exception as exc:  # BudgetExceeded / LlmError
            log.warning("escalation %s: re-synthesis failed: %s", esc_id, exc)
            out = {"status": "failed", "error": str(exc)[:300]}
        conclusion_id = out.get("id") if out.get("status") in ("ok", "held") else None
        detail["synthesis"] = {
            "status": out.get("status"),
            "error": (str(out["error"])[:300] if out.get("error") else None),
            "conclusion_id": conclusion_id,
        }
    detail["finished_at"] = _stamp()
    status = "done" if conclusion_id is not None else "failed"

    # 4. Record the outcome, then the result alert.
    with write_tx(engine) as conn:
        conn.execute(
            update(escalations)
            .where(escalations.c.id == esc_id)
            .values(
                status=status,
                research_run_id=research_run_id,
                conclusion_id=conclusion_id,
                detail=json.dumps(detail, sort_keys=True),
                finished_at=detail["finished_at"],
            )
        )
    concl = None
    if conclusion_id is not None:
        with engine.connect() as conn:
            concl = conn.execute(
                select(
                    conclusions.c.id,
                    conclusions.c.stance,
                    conclusions.c.proposed_stance,
                    conclusions.c.held,
                    conclusions.c.hold_reason,
                    conclusions.c.confidence,
                ).where(conclusions.c.id == conclusion_id)
            ).first()
    deps.notify([result_alert(c, detail, concl)])
    return status


def run_escalations(engine: Engine, deps: EscalationDeps, now: datetime | None = None) -> JobResult:
    """One pass. Returns rows written = escalations claimed (run or refused)."""
    cooldown = timedelta(hours=deps.params.cooldown_hours)
    now = now or datetime.now(UTC)
    todo = candidates(engine, deps.params, deps.symbols, now, deps.lookback_days)
    if not todo:
        return JobResult()
    seen: list[Recent] = recent(engine, now, cooldown)
    ran: list[str] = []
    failed: list[str] = []
    refused: list[str] = []
    for c in todo:
        why = refusal(c.symbol, now, seen, max_per_day=deps.max_per_day, cooldown=cooldown)
        esc_id = _claim(engine, c, why, now)
        label = f"{c.symbol}#{c.event_id}"
        if why:
            log.info("escalation %s refused: %s", label, why)
            refused.append(f"{label} ({why})")
            continue
        seen.append(Recent(c.symbol, to_iso(now)))
        day_count = sum(1 for r in seen if r.created_at >= to_iso(sgt_day_start(now)))
        deps.notify([start_alert(c, day_count, deps.max_per_day)])
        status = escalate_one(engine, deps, c, esc_id)
        (ran if status == "done" else failed).append(label)
        log.info("escalation %s %s", label, status)
    notes = []
    if failed:
        notes.append("failed: " + ", ".join(failed))
    if refused:
        notes.append("refused: " + ", ".join(refused))
    return JobResult(
        rows_written=len(ran) + len(failed) + len(refused),
        warning="; ".join(notes) or None,
    )
