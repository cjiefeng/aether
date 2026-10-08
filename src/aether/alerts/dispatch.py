"""The `alerts` job: candidates → outbox → Telegram (spec M3). Outbox pattern:

1. Collect candidates (reads only).
2. One short `write_tx` inserts the new ones (`ON CONFLICT(dedupe_key) DO NOTHING`), so a re-run
   never duplicates. With Telegram enabled they are `telegram`/`pending`; otherwise
   `dashboard`/`dashboard_only` and the dashboard is the only place they appear.
3. Pending rows are sent one at a time, **outside** any transaction, and each outcome is recorded
   in its own short `write_tx`. Undelivered rows expire after `pending_expiry_hours` instead of
   arriving late; a row that keeps failing is marked `failed` after `max_attempts`.

M13 notification policy (spec §5.2.6):
- **One message per event.** `risk_event`, `off_cycle_review` and `escalation` alerts for the same
  event share one Telegram message. The first becomes the primary (RISK first, then escalation,
  then off-cycle); its text carries every label ("RISK · ACME · dilution (materiality 4/5) ·
  off-cycle review suggested · escalated"). The others are stored with `delivery = merged`
  (dashboard only, `payload.merged_into` = the primary's dedupe key). A label that arrives while
  the primary is still pending is added to it; after it was sent, the new row is only recorded.
- **Immediate vs digest.** Only `delivery = immediate` rows are sent by `deliver`. `digest` rows
  wait for `run_digest` at `digest_time`, which sends one `digest` message listing them and marks
  them `digested`. An empty digest isn't sent.
- `delivery = dashboard_only` rows (Telegram off, or an unchanged escalation result) never send.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from aether.alerts.candidates import MAX_TEXT, SGT, AlertCandidate, collect
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


MERGE_KINDS = ("risk_event", "escalation", "off_cycle_review")  # primary preference order
LABEL_ORDER = ("risk_event", "off_cycle_review", "escalation")  # labels on the first line


def merge_text(base: str, others: Sequence[AlertCandidate]) -> str:
    """`base` with the others' labels on its first line and their extra lines appended (lines
    already present, e.g. the shared title, are skipped). Plain text, at most 4096 chars."""
    lines = base.split("\n")
    head, body = lines[0], lines[1:]
    order = {k: i for i, k in enumerate(LABEL_ORDER)}
    for o in sorted(others, key=lambda c: order.get(c.kind, len(order))):
        if o.label and o.label not in head:
            head += f" · {o.label}"
        for line in o.text.split("\n")[1:]:
            if line and line not in body and line != head:
                body.append(line)
    text = "\n".join([head, *body])
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _row(c: AlertCandidate, *, telegram: bool, created: str) -> dict[str, Any]:
    delivery = c.delivery if telegram else "dashboard_only"
    return {
        "event_id": c.event_id,
        "kind": c.kind,
        "channel": "telegram" if telegram else "dashboard",
        "status": "dashboard_only" if delivery == "dashboard_only" else "pending",
        "delivery": delivery,
        "text": c.text,
        "created_at": created,
        "payload": json.dumps(c.payload, sort_keys=True),
        "dedupe_key": c.dedupe_key,
    }


def _merged(c: AlertCandidate, primary_key: str, created: str) -> dict[str, Any]:
    row = _row(c, telegram=True, created=created)
    row.update(
        status="dashboard_only",
        delivery="merged",
        payload=json.dumps({**c.payload, "merged_into": primary_key}, sort_keys=True),
    )
    return row


def _merge_event(
    conn: Connection, group: list[AlertCandidate], created: str
) -> list[dict[str, Any]]:
    """Rows for one event's new merge-kind candidates (Telegram on); may update the primary."""
    primary = conn.execute(
        select(alerts.c.id, alerts.c.dedupe_key, alerts.c.status, alerts.c.delivery, alerts.c.text)
        .where(
            alerts.c.event_id == group[0].event_id,
            alerts.c.kind.in_(MERGE_KINDS),
            alerts.c.delivery.in_(("immediate", "digest")),
        )
        .order_by(alerts.c.id)
        .limit(1)
    ).first()
    urgent = any(c.delivery == "immediate" for c in group)
    if primary is not None:
        if primary.status == "pending":
            values: dict[str, Any] = {"text": merge_text(primary.text, group)}
            if urgent:
                values["delivery"] = "immediate"
            conn.execute(update(alerts).where(alerts.c.id == primary.id).values(**values))
        return [_merged(c, primary.dedupe_key, created) for c in group]
    group = sorted(group, key=lambda c: MERGE_KINDS.index(c.kind))
    first, rest = group[0], group[1:]
    row = _row(first, telegram=True, created=created)
    row["text"] = merge_text(first.text, rest)
    row["delivery"] = "immediate" if urgent else "digest"
    row["status"] = "pending"
    return [row, *(_merged(c, first.dedupe_key, created) for c in rest)]


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
    new: list[AlertCandidate] = []
    for c in candidates:
        if c.dedupe_key not in existing:
            existing.add(c.dedupe_key)
            new.append(c)
    if not new:
        return 0
    created = to_iso(now)
    with write_tx(engine) as conn:
        rows: list[dict[str, Any]] = []
        groups: dict[int, list[AlertCandidate]] = {}
        for c in new:
            if telegram and c.event_id is not None and c.kind in MERGE_KINDS:
                groups.setdefault(c.event_id, []).append(c)
            else:
                rows.append(_row(c, telegram=telegram, created=created))
        for group in groups.values():
            rows += _merge_event(conn, group, created)
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
            .where(
                alerts.c.status == "pending",
                alerts.c.channel == "telegram",
                alerts.c.delivery == "immediate",
            )
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
    llm_budget_alert: tuple[Decimal, Decimal] | None = None,
    extra: Sequence[AlertCandidate] = (),
) -> JobResult:
    """`extra` (M13): candidates from another job, e.g. an escalation, enqueued in the same batch
    as the collected ones so an event's RISK, off-cycle and escalation alerts merge."""
    now = now or datetime.now(UTC)
    telegram = service is not None and service.blocked is None
    candidates = collect(engine, cfg, params, now, off_cycle_min_materiality, llm_budget_alert)
    candidates += extra
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


DIGEST_LINE_MAX = 300


def digest_text(day: str, lines: Sequence[str]) -> tuple[str, int]:
    """(text, how many lines fit). Plain text, at most 4096 chars, with an overflow pointer."""
    header = f"Daily digest · {day} · {len(lines)} alert(s) since the last digest"
    out = [header]
    size = len(header)
    for i, line in enumerate(lines):
        item = "• " + _clip(line, DIGEST_LINE_MAX)
        more = f"… and {len(lines) - i} more on the Alerts page."
        if size + 1 + len(item) + 1 + len(more) > MAX_TEXT:
            out.append(more)
            return "\n".join(out), i
        out.append(item)
        size += 1 + len(item)
    return "\n".join(out), len(lines)


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def build_digest(engine: Engine, now: datetime) -> int | None:
    """Fold pending `digest` rows into one `digest` alert (immediate). Returns its id, or None when
    there was nothing to digest (an empty digest isn't sent) or today's digest already exists."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(alerts.c.id, alerts.c.text)
            .where(
                alerts.c.status == "pending",
                alerts.c.channel == "telegram",
                alerts.c.delivery == "digest",
            )
            .order_by(alerts.c.id)
        ).all()
    if not rows:
        return None
    day = now.astimezone(SGT).date().isoformat()
    text, _ = digest_text(day, [r.text.split("\n")[0] for r in rows])
    ids = [r.id for r in rows]
    key = f"digest:{day}"
    with write_tx(engine) as conn:
        if conn.execute(select(alerts.c.id).where(alerts.c.dedupe_key == key)).first():
            return None
        digest_id = int(
            conn.execute(
                alerts.insert()
                .values(
                    kind="digest",
                    channel="telegram",
                    status="pending",
                    delivery="immediate",
                    text=text,
                    created_at=to_iso(now),
                    payload=json.dumps({"alert_ids": ids}),
                    dedupe_key=key,
                )
                .returning(alerts.c.id)
            ).scalar_one()
        )
        conn.execute(
            update(alerts)
            .where(alerts.c.id.in_(ids), alerts.c.status == "pending")
            .values(status="digested")
        )
    return digest_id


def run_digest(
    engine: Engine,
    cfg: AlertsConfig,
    service: TelegramService | None,
    disabled_reason: str | None,
    *,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> JobResult:
    """The daily digest (spec §5.2.6, default 08:00 SGT)."""
    now = now or datetime.now(UTC)
    if service is None:
        return JobResult(provider="dashboard", warning=disabled_reason)
    digest_id = build_digest(engine, now)
    if digest_id is None:
        return JobResult(provider="telegram", warning="nothing to digest")
    result = deliver(engine, service, cfg, now, sleep)
    return JobResult(rows_written=1 + result.sent, provider="telegram", warning=result.warning)


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
