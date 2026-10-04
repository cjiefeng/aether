"""Scheduler wiring and jobs. All jobs run in the single worker (`max_instances=1`).

Every job runs through `run_job` (aether/runs.py), which records a `job_runs` row. Network I/O
and LLM calls happen outside write transactions.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import Engine, delete, select, update

from aether.config import Settings
from aether.db.engine import write_tx
from aether.db.models import commands, job_runs
from aether.db.types import to_iso, utcnow_iso
from aether.ingest.prices import ingest_prices
from aether.ingest.qtum_holdings import ingest_qtum_holdings
from aether.market import last_ok_finished
from aether.ops.backup import backup
from aether.providers.prices import FailoverPriceProvider, MassiveProvider, YFinanceProvider
from aether.runs import JobResult, run_job

__all__ = ["JobResult", "build_scheduler", "process_commands", "run_job"]

log = logging.getLogger(__name__)

TZ = "Asia/Singapore"
HEARTBEAT_MINUTES = 10


def heartbeat() -> JobResult:
    return JobResult()


CommandHandler = Callable[[dict[str, Any]], dict[str, Any]]

# Handlers for dashboard-requested commands. Each returns a JSON-serialisable result.
# Handlers that need the engine/providers are added in `build_scheduler`.
COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "ping": lambda _args: {"pong": utcnow_iso()},
}


def process_commands(
    engine: Engine, batch: int = 20, handlers: Mapping[str, CommandHandler] | None = None
) -> JobResult:
    handlers = COMMAND_HANDLERS if handlers is None else handlers
    with engine.connect() as conn:
        pending = conn.execute(
            select(commands.c.id, commands.c.kind, commands.c.args)
            .where(commands.c.status == "pending")
            .order_by(commands.c.id)
            .limit(batch)
        ).all()
    done = 0
    for cmd_id, kind, args in pending:
        handler = handlers.get(kind)
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


def make_price_provider(settings: Settings) -> FailoverPriceProvider:
    fallback = None
    if settings.massive_api_key is not None:
        fallback = MassiveProvider(settings.massive_api_key.get_secret_value())
    else:
        log.warning("MASSIVE_API_KEY not set: prices have no fallback provider")
    return FailoverPriceProvider(YFinanceProvider(), fallback)


def prices_job(engine: Engine, provider: FailoverPriceProvider) -> JobResult:
    # Runs 06:30 SGT, after the US close. Sunday's run re-fetches the full 2 years.
    sunday = datetime.now(ZoneInfo(TZ)).weekday() == 6
    return ingest_prices(engine, provider, full_refresh=sunday)


def qtum_holdings_job(engine: Engine) -> JobResult:
    with httpx.Client(timeout=30) as client:
        return ingest_qtum_holdings(engine, client)


CATCH_UP_AFTER = timedelta(hours=24)


def _catch_up(engine: Engine, job: str) -> dict[str, Any]:
    """Extra `add_job` kwargs: run now if the last ok run is older than a day. Otherwise none,
    and the cron trigger decides. (APScheduler 3: `next_run_time=None` would mean *paused*.)"""
    last = last_ok_finished(engine, job)
    if last is None or datetime.now(UTC) - datetime.fromisoformat(last) > CATCH_UP_AFTER:
        return {"next_run_time": datetime.now(ZoneInfo(TZ))}
    return {}


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

    provider = make_price_provider(settings)
    # The cron job and the dashboard's refresh command share one provider (failover state,
    # rate limiter); never run them concurrently on the scheduler's thread pool.
    prices_lock = threading.Lock()

    def run_prices() -> None:
        with prices_lock:
            run_job(engine, "prices", lambda: prices_job(engine, provider))

    def refresh_prices(_args: dict[str, Any]) -> dict[str, Any]:
        if not prices_lock.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            result = run_job(engine, "prices", lambda: prices_job(engine, provider))
        finally:
            prices_lock.release()
        return {"ok": result is not None, "rows": result.rows_written if result else 0}

    run_holdings = wrap("qtum_holdings", lambda: qtum_holdings_job(engine))

    handlers = {**COMMAND_HANDLERS, "refresh_prices": refresh_prices}

    def commands_tick() -> None:
        # Only record a job_runs row when there is work, to keep the table small.
        if has_pending_commands(engine):
            run_job(engine, "process_commands", lambda: process_commands(engine, handlers=handlers))

    sched.add_job(
        wrap("heartbeat", heartbeat),
        "interval",
        minutes=HEARTBEAT_MINUTES,
        id="heartbeat",
        next_run_time=datetime.now(ZoneInfo(TZ)),
    )
    sched.add_job(commands_tick, "interval", seconds=30, id="process_commands")
    # Prices + QTUM holdings: daily 06:30 SGT (spec §9); catch up at startup if >24h stale.
    sched.add_job(
        run_prices,
        "cron",
        hour=6,
        minute=30,
        id="prices",
        **_catch_up(engine, "prices"),
    )
    sched.add_job(
        run_holdings,
        "cron",
        hour=6,
        minute=35,
        id="qtum_holdings",
        **_catch_up(engine, "qtum_holdings"),
    )
    sched.add_job(
        wrap("nightly_maintenance", lambda: nightly_maintenance(engine, settings)),
        "cron",
        hour=4,
        minute=0,
        id="nightly_maintenance",
    )
    return sched
