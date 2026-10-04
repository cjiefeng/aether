"""Scheduler wiring and the M0 jobs. All jobs run in the single worker (`max_instances=1`).

Every job runs through `run_job`, which records a `job_runs` row. Network I/O and LLM calls
happen outside write transactions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import Engine, delete, insert, select, update

from aether.config import Settings
from aether.db.engine import write_tx
from aether.db.models import commands, job_runs
from aether.db.types import to_iso, utcnow_iso
from aether.ops.backup import backup

log = logging.getLogger(__name__)

TZ = "Asia/Singapore"
HEARTBEAT_MINUTES = 10


@dataclass(frozen=True)
class JobResult:
    rows_written: int = 0
    provider: str | None = None


def run_job(engine: Engine, name: str, fn: Callable[[], JobResult]) -> JobResult | None:
    with write_tx(engine) as conn:
        run_id = conn.execute(
            insert(job_runs)
            .values(job=name, started_at=utcnow_iso(), status="running")
            .returning(job_runs.c.id)
        ).scalar_one()
    try:
        result = fn()
    except Exception as exc:
        log.exception("job %s failed", name)
        with write_tx(engine) as conn:
            conn.execute(
                update(job_runs)
                .where(job_runs.c.id == run_id)
                .values(finished_at=utcnow_iso(), status="failed", error=repr(exc)[:500])
            )
        return None
    with write_tx(engine) as conn:
        conn.execute(
            update(job_runs)
            .where(job_runs.c.id == run_id)
            .values(
                finished_at=utcnow_iso(),
                status="ok",
                rows_written=result.rows_written,
                provider=result.provider,
            )
        )
    return result


def heartbeat() -> JobResult:
    return JobResult()


# Handlers for dashboard-requested commands. Each returns a JSON-serialisable result.
COMMAND_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "ping": lambda _args: {"pong": utcnow_iso()},
}


def process_commands(engine: Engine, batch: int = 20) -> JobResult:
    with engine.connect() as conn:
        pending = conn.execute(
            select(commands.c.id, commands.c.kind, commands.c.args)
            .where(commands.c.status == "pending")
            .order_by(commands.c.id)
            .limit(batch)
        ).all()
    done = 0
    for cmd_id, kind, args in pending:
        handler = COMMAND_HANDLERS.get(kind)
        if handler is None:
            status, result = "rejected", {"error": f"unknown command kind {kind!r}"}
        else:
            try:
                status, result = "done", handler(json.loads(args))
            except Exception as exc:
                log.exception("command %s (%s) failed", cmd_id, kind)
                status, result = "failed", {"error": repr(exc)[:500]}
        with write_tx(engine) as conn:
            conn.execute(
                update(commands)
                .where(commands.c.id == cmd_id, commands.c.status == "pending")
                .values(status=status, processed_at=utcnow_iso(), result=json.dumps(result))
            )
        done += 1
    return JobResult(rows_written=done)


JOB_RUNS_RETENTION_DAYS = 90


def nightly_maintenance(engine: Engine, settings: Settings) -> JobResult:
    backup(settings.db_path, settings.resolved_backup_dir, keep_days=settings.backup_keep_days)
    cutoff = to_iso(datetime.now(UTC) - timedelta(days=JOB_RUNS_RETENTION_DAYS))
    with write_tx(engine) as conn:
        pruned = conn.execute(delete(job_runs).where(job_runs.c.started_at < cutoff)).rowcount
    # Outside any transaction: checkpoint can't complete inside one.
    raw = engine.raw_connection()
    try:
        raw.driver_connection.execute("PRAGMA optimize")  # type: ignore[union-attr]
        raw.driver_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # type: ignore[union-attr]
    finally:
        raw.close()
    return JobResult(rows_written=pruned)


def has_pending_commands(engine: Engine) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(commands.c.id).where(commands.c.status == "pending").limit(1)
            ).first()
            is not None
        )


def build_scheduler(engine: Engine, settings: Settings) -> Any:
    sched = BlockingScheduler(
        timezone=TZ, job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
    )

    def wrap(name: str, fn: Callable[[], JobResult]) -> Callable[[], None]:
        def _run() -> None:
            run_job(engine, name, fn)

        return _run

    def commands_tick() -> None:
        # Only record a job_runs row when there is work, to keep the table small.
        if has_pending_commands(engine):
            run_job(engine, "process_commands", lambda: process_commands(engine))

    sched.add_job(
        wrap("heartbeat", heartbeat),
        "interval",
        minutes=HEARTBEAT_MINUTES,
        id="heartbeat",
        next_run_time=datetime.now(ZoneInfo(TZ)),
    )
    sched.add_job(commands_tick, "interval", seconds=30, id="process_commands")
    sched.add_job(
        wrap("nightly_maintenance", lambda: nightly_maintenance(engine, settings)),
        "cron",
        hour=4,
        minute=0,
        id="nightly_maintenance",
    )
    return sched
