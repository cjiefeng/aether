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
from aether.config import LlmConfig
from aether.db.models import alerts, escalations, eval_runs, events, job_runs, llm_calls
from aether.db.types import micros_sum, micros_to_decimal, to_iso
from aether.escalate.spend import escalation_spent_since
from aether.llm.pricing import SGT, sgt_day_start
from aether.ops import llm_log
from aether.ops.backup import NAME_RE

SPEND_DAYS = 30
# Spec §10 (M15): normal running after M8-M15, USD per day.
SPEND_TARGET_USD = (Decimal("0.60"), Decimal("0.90"))
LOG_LIMIT = 100
STATUSES = ("ok", "error", "refusal", "refused_budget", "batch_submitted", "batch_result")


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
                func.count().filter(llm_calls.c.status.in_(("ok", "refusal"))),
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


# --------------------------------------------------------------------------- M15 expense log


@dataclass(frozen=True)
class ComponentSpend:
    """Last SPEND_DAYS days by purpose and cost component (spec §10), plus the TOTAL row."""

    rows: list[llm_log.SummaryRow]
    total: llm_log.SummaryRow | None
    per_day: Decimal
    target: tuple[Decimal, Decimal]
    price_version: str

    @property
    def on_target(self) -> bool:
        return self.per_day <= self.target[1]


def component_spend(engine: Engine, cfg: LlmConfig, now: datetime | None = None) -> ComponentSpend:
    today = (now or datetime.now(UTC)).astimezone(SGT).date()
    data = llm_log.rows(engine, today - timedelta(days=SPEND_DAYS - 1), today)
    rows = llm_log.purpose_summary(data, cfg, label=f"last {SPEND_DAYS} days")
    total = rows.pop() if rows else None  # the TOTAL row comes last
    per_day = (total.cost / SPEND_DAYS).quantize(Decimal("0.01")) if total else Decimal(0)
    return ComponentSpend(rows, total, per_day, SPEND_TARGET_USD, cfg.price_version)


@dataclass(frozen=True)
class LogFilters:
    d_from: str
    d_to: str
    purpose: str = ""
    model: str = ""
    status: str = ""
    error: str | None = None


@dataclass(frozen=True)
class ExpenseLog:
    filters: LogFilters
    rows: list[dict[str, Any]]
    total: int  # rows matching the filters (only the latest LOG_LIMIT are shown)
    purposes: list[str]
    models: list[str]
    statuses: tuple[str, ...] = STATUSES


def expense_log(
    engine: Engine,
    *,
    d_from: str | None,
    d_to: str | None,
    purpose: str | None,
    model: str | None,
    status: str | None,
    now: datetime | None = None,
) -> ExpenseLog:
    today = (now or datetime.now(UTC)).astimezone(SGT).date()
    error = None
    try:
        lo, hi = llm_log.parse_range(d_from or None, d_to or None, today)
    except llm_log.RangeError as exc:
        error = str(exc)
        lo, hi = llm_log.parse_range(None, None, today)
    data = llm_log.rows(engine, lo, hi)
    purposes = sorted({str(r["purpose"]) for r in data})
    models = sorted({str(r["model"]) for r in data})
    keep = [
        r
        for r in data
        if (not purpose or r["purpose"] == purpose)
        and (not model or r["model"] == model)
        and (not status or r["status"] == status)
    ]
    shown = [
        {**r, "context": json.loads(r["context"] or "{}")} for r in reversed(keep[-LOG_LIMIT:])
    ]
    f = LogFilters(lo.isoformat(), hi.isoformat(), purpose or "", model or "", status or "", error)
    return ExpenseLog(f, shown, len(keep), purposes, models)
