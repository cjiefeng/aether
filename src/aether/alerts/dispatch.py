"""The `alerts` job: candidates → outbox → Telegram (spec M3). Outbox pattern:

1. Collect candidates (reads only).
2. One short `write_tx` inserts the new ones (`ON CONFLICT(dedupe_key) DO NOTHING`), so a re-run
   never duplicates. With Telegram enabled they are `telegram`/`pending`; otherwise
   `dashboard`/`dashboard_only` and the dashboard is the only place they appear.
3. Pending rows are sent one at a time, **outside** any transaction, and each outcome is recorded
   in its own short `write_tx`. Undelivered rows expire after `pending_expiry_hours` instead of
   arriving late; a row that keeps failing is marked `failed` after `max_attempts`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select, update

from aether.alerts.candidates import AlertCandidate, collect
from aether.alerts.telegram import TelegramError, TelegramService
from aether.config import AlertsConfig, RiskFlagParams
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import alerts
from aether.db.types import to_iso
from aether.runs import JobResult

log = logging.getLogger(__name__)

SEND_INTERVAL_S = 1.0  # Telegram allows about one message per second to a chat


@dataclass(frozen=True)
class DeliveryResult:
    sent: int = 0
    failed: int = 0
    expired: int = 0
    warning: str | None = None


def enqueue(
    engine: Engine, candidates: Sequence[AlertCandidate], *, telegram: bool, now: datetime
) -> int:
    """Insert candidates not seen before. Returns how many were new."""
    if not candidates:
        return 0
    keys = [c.dedupe_key for c in candidates]
    with engine.connect() as conn:
        existing = set(
            conn.execute(select(alerts.c.dedupe_key).where(alerts.c.dedupe_key.in_(keys))).scalars()
        )
    new = [c for c in candidates if c.dedupe_key not in existing]
    if not new:
        return 0
    created = to_iso(now)
    rows = [
        {
            "event_id": c.event_id,
            "kind": c.kind,
            "channel": "telegram" if telegram else "dashboard",
            "status": "pending" if telegram else "dashboard_only",
            "text": c.text,
            "created_at": created,
            "payload": json.dumps(c.payload, sort_keys=True),
            "dedupe_key": c.dedupe_key,
        }
        for c in new
    ]
    with write_tx(engine) as conn:
        upsert(conn, alerts, rows, key_cols=["dedupe_key"], update_cols=[])
    return len(new)


def deliver(
    engine: Engine,
    service: TelegramService,
    cfg: AlertsConfig,
    now: datetime,
    sleep: Callable[[float], None] = time.sleep,
) -> DeliveryResult:
    expire_before = to_iso(now - timedelta(hours=cfg.pending_expiry_hours))
    with write_tx(engine) as conn:
        expired = conn.execute(
            update(alerts)
            .where(alerts.c.status == "pending", alerts.c.created_at < expire_before)
            .values(status="expired", last_error="undelivered before pending_expiry_hours")
        ).rowcount
    with engine.connect() as conn:
        pending = conn.execute(
            select(alerts.c.id, alerts.c.text, alerts.c.attempts)
            .where(alerts.c.status == "pending", alerts.c.channel == "telegram")
            .order_by(alerts.c.id)
            .limit(cfg.max_sends_per_run)
        ).all()
    if not pending:
        return DeliveryResult(expired=expired)

    try:
        allowed = service.verify(now)
    except TelegramError as exc:
        # Transient (network, Bot API down): rows stay pending for the next run.
        return DeliveryResult(expired=expired, warning=f"telegram unreachable: {exc}"[:500])
    if not allowed:
        with write_tx(engine) as conn:
            conn.execute(
                update(alerts)
                .where(alerts.c.id.in_([p.id for p in pending]))
                .values(status="failed", last_error=(service.blocked or "not allowed")[:500])
            )
        return DeliveryResult(failed=len(pending), expired=expired, warning=service.blocked)

    sent = failed = 0
    warning = None
    for i, (alert_id, text, attempts) in enumerate(pending):
        if i:
            sleep(SEND_INTERVAL_S)
        try:
            service.bot.send_message(text)
        except TelegramError as exc:
            attempts += 1
            status = "failed" if attempts >= cfg.max_attempts else "pending"
            failed += status == "failed"
            warning = f"send failed: {exc}"[:500]
            with write_tx(engine) as conn:
                conn.execute(
                    update(alerts)
                    .where(alerts.c.id == alert_id)
                    .values(status=status, attempts=attempts, last_error=str(exc)[:500])
                )
            continue
        with write_tx(engine) as conn:
            conn.execute(
                update(alerts)
                .where(alerts.c.id == alert_id)
                .values(
                    status="sent",
                    attempts=attempts + 1,
                    sent_at=to_iso(datetime.now(UTC)),
                    last_error=None,
                )
            )
        sent += 1
    return DeliveryResult(sent=sent, failed=failed, expired=expired, warning=warning)


def run_alerts(
    engine: Engine,
    cfg: AlertsConfig,
    params: RiskFlagParams,
    service: TelegramService | None,
    disabled_reason: str | None,
    *,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    off_cycle_min_materiality: int | None = None,
) -> JobResult:
    now = now or datetime.now(UTC)
    telegram = service is not None and service.blocked is None
    candidates = collect(engine, cfg, params, now, off_cycle_min_materiality)
    new = enqueue(engine, candidates, telegram=telegram, now=now)
    if service is None:
        log.info("alerts: %d new (dashboard only: %s)", new, disabled_reason)
        return JobResult(rows_written=new, provider="dashboard", warning=disabled_reason)
    result = deliver(engine, service, cfg, now, sleep)
    log.info(
        "alerts: %d new, %d sent, %d failed, %d expired",
        new,
        result.sent,
        result.failed,
        result.expired,
    )
    return JobResult(
        rows_written=new + result.sent,
        provider="telegram" if service.blocked is None else "dashboard",
        warning=result.warning or service.blocked,
    )


def enqueue_test_alert(engine: Engine, *, telegram: bool, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    stamp = to_iso(now)
    key = f"test:{stamp}"
    enqueue(
        engine,
        [
            AlertCandidate(
                kind="test",
                dedupe_key=key,
                text=f"Aether test alert ({stamp}). "
                + (
                    "If you can read this in Telegram, delivery works."
                    if telegram
                    else "Telegram is off: this alert exists on the dashboard only."
                ),
            )
        ],
        telegram=telegram,
        now=now,
    )
    return key
