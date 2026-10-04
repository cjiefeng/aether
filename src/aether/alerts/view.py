"""Read-side views for the Alerts and Facts pages. Nothing here writes; callers pass the `mode=ro`
engine."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import Engine, select

from aether.db.models import alerts, facts, job_runs


@dataclass(frozen=True)
class AlertRow:
    id: int
    kind: str
    channel: str
    status: str
    text: str
    created_at: str
    sent_at: str | None
    attempts: int
    last_error: str | None


def recent_alerts(engine: Engine, limit: int = 100) -> list[AlertRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                alerts.c.id,
                alerts.c.kind,
                alerts.c.channel,
                alerts.c.status,
                alerts.c.text,
                alerts.c.created_at,
                alerts.c.sent_at,
                alerts.c.attempts,
                alerts.c.last_error,
            )
            .order_by(alerts.c.id.desc())
            .limit(limit)
        ).all()
    return [AlertRow(*r) for r in rows]


@dataclass(frozen=True)
class DeliveryStatus:
    last_run: str | None
    status: str | None
    channel: str | None  # telegram | dashboard (job_runs.provider)
    note: str | None  # why Telegram is off, or the last delivery problem


def delivery_status(engine: Engine) -> DeliveryStatus:
    with engine.connect() as conn:
        r = conn.execute(
            select(job_runs.c.started_at, job_runs.c.status, job_runs.c.provider, job_runs.c.error)
            .where(job_runs.c.job == "alerts", job_runs.c.status != "running")
            .order_by(job_runs.c.id.desc())
            .limit(1)
        ).first()
    if r is None:
        return DeliveryStatus(None, None, None, None)
    return DeliveryStatus(*r)


@dataclass(frozen=True)
class FactRow:
    id: str
    claim: str
    sources: tuple[str, ...]
    retrieved_at: str | None
    status: str
    notes: str | None
    open_question: str | None
    synced_at: str


STATUS_LABELS = {
    "unverified": "Unverified",
    "verified_by_claude": "Verified by Claude",
    "signed_off": "Signed off",
}


def facts_rows(engine: Engine) -> list[FactRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                facts.c.id,
                facts.c.claim,
                facts.c.source_urls,
                facts.c.retrieved_at,
                facts.c.status,
                facts.c.notes,
                facts.c.open_question,
                facts.c.synced_at,
            ).order_by(facts.c.id)
        ).all()
    return [
        FactRow(fid, claim, tuple(json.loads(srcs)), ret, status, notes, oq, synced)
        for fid, claim, srcs, ret, status, notes, oq, synced in rows
    ]
