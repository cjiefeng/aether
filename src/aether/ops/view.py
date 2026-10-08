"""Read-only queries for the Ops page (spec §8.13, M11). Nothing here writes or calls an LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select

from aether.alerts.candidates import FailingJob, failing_jobs
from aether.db.models import alerts, escalations, eval_runs, events, job_runs, llm_calls
from aether.db.types import micros_sum, micros_to_decimal, to_iso
from aether.escalate.spend import escalation_spent_since
from aether.llm.pricing import sgt_day_start
from aether.ops.backup import NAME_RE

SPEND_DAYS = 30


@dataclass(frozen=True)
class PurposeSpend:
    purpose: str
    calls: int
    cost: Decimal


@dataclass(frozen=True)
class EscalationRow:
    id: int
    event_id: int
    title: str | None
    symbol: str
    trigger: str
    status: str
    refusal: str | None
    conclusion_id: int | None
    verify: str | None
    created_at: str
    reason: str | None = None  # M13: why it escalated
    changed: tuple[str, ...] = ()  # M13: what the result changed (empty: dashboard only)


@dataclass(frozen=True)
class Escalations:
    used_today: int
    refused_today: int
    max_per_day: int
    recent: list[EscalationRow]
    spent_today: Decimal = Decimal(0)  # M13: escalation calls today vs the sub-budget
    budget: Decimal = Decimal(0)
    refusals: dict[str, int] | None = None  # M13: by reason, last SPEND_DAYS days


@dataclass(frozen=True)
class MessageCounts:
    """M13 (spec §5.2.6): Telegram messages in the last SPEND_DAYS days."""

    sent: int
    merged: int
    digested: int
    digests: int
    dashboard_only: int


@dataclass(frozen=True)
class EvalRow:
    prompt_version: str
    model: str
    created_at: str
    n_items: int
    provisional: bool
    passed: bool
    class_agreement: float | None
    risk_recall: float | None
    adversarial: str


@dataclass(frozen=True)
class Backups:
    newest: str | None
    newest_age_h: float | None
    count: int
    drill_status: str | None
    drill_at: str | None
    drill_note: str | None


def failing(engine: Engine, hours: int, now: datetime | None = None) -> list[FailingJob]:
    with engine.connect() as conn:
        return failing_jobs(conn, hours, now or datetime.now(UTC))


def spend_by_purpose(engine: Engine, now: datetime | None = None) -> list[PurposeSpend]:
    since = to_iso((now or datetime.now(UTC)) - timedelta(days=SPEND_DAYS))
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                llm_calls.c.purpose,
                func.count().filter(llm_calls.c.status == "ok"),
                micros_sum(llm_calls.c.cost_micros),
            )
            .where(llm_calls.c.created_at >= since)
            .group_by(llm_calls.c.purpose)
            .order_by(llm_calls.c.purpose)
        ).all()
    return [PurposeSpend(p, int(n), micros_to_decimal(int(c))) for p, n, c in rows]


def escalation_summary(
    engine: Engine,
    max_per_day: int,
    now: datetime | None = None,
    limit: int = 20,
    budget: Decimal = Decimal(0),
) -> Escalations:
    now = now or datetime.now(UTC)
    start = sgt_day_start(now)
    day = to_iso(start)
    since = to_iso(now - timedelta(days=SPEND_DAYS))
    with engine.connect() as conn:
        spent = escalation_spent_since(conn, start)
        refusals = {
            str(reason): int(n)
            for reason, n in conn.execute(
                select(escalations.c.refusal, func.count())
                .where(escalations.c.status == "refused", escalations.c.created_at >= since)
                .group_by(escalations.c.refusal)
                .order_by(escalations.c.refusal)
            ).all()
        }
        used, refused = conn.execute(
            select(
                func.count().filter(escalations.c.status != "refused"),
                func.count().filter(escalations.c.status == "refused"),
            ).where(escalations.c.created_at >= day)
        ).one()
        rows = conn.execute(
            select(escalations, events.c.title)
            .join(events, events.c.id == escalations.c.event_id)
            .order_by(escalations.c.id.desc())
            .limit(limit)
        ).all()
    recent = []
    for r in rows:
        detail: dict[str, Any] = json.loads(r.detail)
        recent.append(
            EscalationRow(
                id=r.id,
                event_id=r.event_id,
                title=r.title,
                symbol=r.symbol,
                trigger=r.trigger,
                status=r.status,
                refusal=r.refusal,
                conclusion_id=r.conclusion_id,
                verify=(detail.get("verify") or {}).get("status"),
                created_at=r.created_at,
                reason=detail.get("reason"),
                changed=tuple(detail.get("changed") or ()),
            )
        )
    return Escalations(int(used), int(refused), max_per_day, recent, spent, budget, refusals)


def message_counts(engine: Engine, now: datetime | None = None) -> MessageCounts:
    since = to_iso((now or datetime.now(UTC)) - timedelta(days=SPEND_DAYS))
    with engine.connect() as conn:
        r = conn.execute(
            select(
                func.count().filter(alerts.c.status == "sent", alerts.c.kind != "digest"),
                func.count().filter(alerts.c.delivery == "merged"),
                func.count().filter(alerts.c.status == "digested"),
                func.count().filter(alerts.c.kind == "digest", alerts.c.status == "sent"),
                func.count().filter(
                    alerts.c.delivery == "dashboard_only", alerts.c.channel == "telegram"
                ),
            ).where(alerts.c.created_at >= since)
        ).one()
    return MessageCounts(*(int(x) for x in r))


def eval_scores(engine: Engine, limit: int = 20) -> list[EvalRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(eval_runs).order_by(eval_runs.c.created_at.desc()).limit(limit)
        ).all()
    out = []
    for r in rows:
        m = json.loads(r.metrics)
        adv = m.get("adversarial") or {}
        out.append(
            EvalRow(
                prompt_version=r.prompt_version,
                model=r.model,
                created_at=r.created_at,
                n_items=r.n_items,
                provisional=bool(r.provisional),
                passed=bool(r.passed),
                class_agreement=m.get("class_agreement"),
                risk_recall=m.get("risk_recall"),
                adversarial=f"{adv.get('flagged', 0)}/{adv.get('n', 0)}",
            )
        )
    return out


def backups(engine: Engine, backup_dir: Path, now: datetime | None = None) -> Backups:
    now = now or datetime.now(UTC)
    try:
        files = sorted(p for p in backup_dir.iterdir() if NAME_RE.match(p.name))
    except OSError:
        files = []
    newest = files[-1] if files else None
    age = None
    if newest is not None:
        try:
            mtime = datetime.fromtimestamp(newest.stat().st_mtime, UTC)
            age = round((now - mtime).total_seconds() / 3600, 1)
        except OSError:
            age = None
    with engine.connect() as conn:
        drill = conn.execute(
            select(job_runs.c.status, job_runs.c.started_at, job_runs.c.error, job_runs.c.provider)
            .where(job_runs.c.job == "restore_drill")
            .order_by(job_runs.c.id.desc())
            .limit(1)
        ).first()
    return Backups(
        newest=newest.name if newest else None,
        newest_age_h=age,
        count=len(files),
        drill_status=drill.status if drill else None,
        drill_at=drill.started_at if drill else None,
        drill_note=(drill.error or drill.provider) if drill else None,
    )
