"""Scheduler wiring and jobs. All jobs run in the single worker (`max_instances=1`).

Every job runs through `run_job` (aether/runs.py), which records a `job_runs` row. Network I/O
and LLM calls happen outside write transactions.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import Engine, delete, select, update

from aether.alerts.dispatch import enqueue_test_alert, run_alerts
from aether.alerts.telegram import TelegramBot, TelegramConfig, TelegramError, TelegramService
from aether.config import Settings, load_alerts_config, load_rubric, load_strategies
from aether.db.engine import write_tx
from aether.db.models import commands, job_runs
from aether.db.types import to_iso, utcnow_iso
from aether.ingest.dividends import ingest_dividends
from aether.ingest.earnings_calendar import ingest_earnings_calendar
from aether.ingest.edgar import ingest_edgar
from aether.ingest.prices import ingest_prices
from aether.ingest.qtum_holdings import ingest_qtum_holdings
from aether.market import last_ok_finished
from aether.ops.backup import backup
from aether.portfolio.job import run_strategies
from aether.providers.dividends import FallbackDividends, MassiveDividends, YFinanceDividends
from aether.providers.edgar import EdgarClient
from aether.providers.prices import FailoverPriceProvider, MassiveProvider, YFinanceProvider
from aether.runs import JobResult, run_job

__all__ = ["JobResult", "build_scheduler", "process_commands", "run_job"]

log = logging.getLogger(__name__)

TZ = "Asia/Singapore"
HEARTBEAT_MINUTES = 10
ALERTS_MINUTES = 10
INBOUND_POLL_SECONDS = 30


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


def make_dividend_provider(settings: Settings) -> FallbackDividends:
    fallback = None
    if settings.massive_api_key is not None:
        fallback = MassiveDividends(settings.massive_api_key.get_secret_value())
    return FallbackDividends(YFinanceDividends(), fallback)


def strategies_job(engine: Engine, settings: Settings) -> JobResult:
    return run_strategies(engine, load_strategies(settings.config_dir))


def qtum_holdings_job(engine: Engine) -> JobResult:
    with httpx.Client(timeout=30) as client:
        return ingest_qtum_holdings(engine, client)


def edgar_job(engine: Engine, settings: Settings) -> JobResult:
    if settings.sec_user_agent is None:
        raise RuntimeError("SEC_USER_AGENT is not set; EDGAR ingest is disabled")
    client = EdgarClient(settings.sec_user_agent)
    try:
        return ingest_edgar(engine, client, load_rubric(settings.config_dir))
    finally:
        client.close()


def earnings_calendar_job(engine: Engine) -> JobResult:
    return ingest_earnings_calendar(engine)


def make_telegram(settings: Settings) -> tuple[TelegramService | None, str | None]:
    """The Telegram module, or (None, reason) when disabled (unset or fail-closed config)."""
    config, reason = TelegramConfig.from_settings(settings)
    if config is None:
        log.info("telegram disabled: %s; alerts appear on the dashboard only", reason)
        return None, reason
    return TelegramService(TelegramBot(config)), None


def alerts_job(
    engine: Engine, settings: Settings, service: TelegramService | None, reason: str | None
) -> JobResult:
    return run_alerts(
        engine,
        load_alerts_config(settings.config_dir),
        load_rubric(settings.config_dir).risk_flags,
        service,
        reason,
    )


CATCH_UP_AFTER = timedelta(hours=24)
PORTFOLIO_CATCH_UP_DELAY = timedelta(minutes=5)


def _catch_up(engine: Engine, job: str, delay: timedelta = timedelta(0)) -> dict[str, Any]:
    """Extra `add_job` kwargs: run now (+ `delay`) if the last ok run is older than a day.
    Otherwise none, and the cron trigger decides. (APScheduler 3: `next_run_time=None` would
    mean *paused*.)"""
    last = last_ok_finished(engine, job)
    if last is None or datetime.now(UTC) - datetime.fromisoformat(last) > CATCH_UP_AFTER:
        return {"next_run_time": datetime.now(ZoneInfo(TZ)) + delay}
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

    edgar_lock = threading.Lock()

    def run_edgar() -> None:
        with edgar_lock:
            run_job(engine, "edgar", lambda: edgar_job(engine, settings))

    def refresh_edgar(_args: dict[str, Any]) -> dict[str, Any]:
        if not edgar_lock.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            result = run_job(engine, "edgar", lambda: edgar_job(engine, settings))
        finally:
            edgar_lock.release()
        return {"ok": result is not None, "rows": result.rows_written if result else 0}

    # Dividends then backtests (M4), daily 07:10 SGT after prices. The command shares the lock.
    dividend_provider = make_dividend_provider(settings)
    portfolio_lock = threading.Lock()

    def portfolio_pipeline() -> JobResult | None:
        run_job(engine, "dividends", lambda: ingest_dividends(engine, dividend_provider))
        # Backtests run even if the dividend fetch failed: stored dividends are still valid.
        return run_job(engine, "strategies", lambda: strategies_job(engine, settings))

    def run_portfolio() -> None:
        with portfolio_lock:
            portfolio_pipeline()

    def recompute_strategies(_args: dict[str, Any]) -> dict[str, Any]:
        if not portfolio_lock.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            result = portfolio_pipeline()
        finally:
            portfolio_lock.release()
        return {"ok": result is not None, "rows": result.rows_written if result else 0}

    telegram, telegram_off = make_telegram(settings)
    # The cron job and the test command share the outbox; never deliver concurrently.
    alerts_lock = threading.Lock()

    def run_alerts_job() -> None:
        with alerts_lock:
            run_job(engine, "alerts", lambda: alerts_job(engine, settings, telegram, telegram_off))

    def test_alert(_args: dict[str, Any]) -> dict[str, Any]:
        on = telegram is not None and telegram.blocked is None
        key = enqueue_test_alert(engine, telegram=on)
        if on and alerts_lock.acquire(blocking=False):
            try:
                run_job(
                    engine, "alerts", lambda: alerts_job(engine, settings, telegram, telegram_off)
                )
            finally:
                alerts_lock.release()
        return {"dedupe_key": key, "channel": "telegram" if on else "dashboard"}

    handlers = {
        **COMMAND_HANDLERS,
        "refresh_prices": refresh_prices,
        "refresh_edgar": refresh_edgar,
        "test_alert": test_alert,
        "recompute_strategies": recompute_strategies,
    }

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
    # EDGAR (spec §9): every 30 min 21:00-05:00 SGT across US sessions (Mon-Fri ET evening
    # spans SGT Mon 21:00 .. Sat 05:00), twice a day otherwise. NYSE holidays arrive in M9.
    sched.add_job(
        run_edgar,
        "cron",
        day_of_week="mon-fri",
        hour="21-23",
        minute="0,30",
        id="edgar_session_evening",
        **_catch_up(engine, "edgar"),
    )
    sched.add_job(
        run_edgar,
        "cron",
        day_of_week="tue-sat",
        hour="0-4",
        minute="0,30",
        id="edgar_session_night",
    )
    sched.add_job(run_edgar, "cron", hour="9,17", minute=0, id="edgar_daytime")
    sched.add_job(
        wrap("earnings_calendar", lambda: earnings_calendar_job(engine)),
        "cron",
        hour=7,
        minute=15,
        id="earnings_calendar",
        **_catch_up(engine, "earnings_calendar"),
    )
    sched.add_job(
        run_portfolio,
        "cron",
        hour=7,
        minute=10,
        id="portfolio",
        # Startup catch-up waits for the prices catch-up (which starts at once) to land first.
        **_catch_up(engine, "strategies", PORTFOLIO_CATCH_UP_DELAY),
    )
    # Alerts (M3): every 10 min; covers the hourly job-health check (spec §9).
    sched.add_job(
        run_alerts_job,
        "interval",
        minutes=ALERTS_MINUTES,
        id="alerts",
        next_run_time=datetime.now(ZoneInfo(TZ)) + timedelta(minutes=2),
    )
    if telegram is not None:
        service = telegram

        last_fail_logged: list[float] = []

        def telegram_inbound() -> None:
            # Long poll. A job_runs row only when updates arrived or the poll failed (failures at
            # most every 10 min, so a Bot API outage doesn't fill job_runs).
            try:
                n = service.poll_inbound()
            except TelegramError as exc:
                log.warning("telegram inbound poll failed: %s", exc)
                now = time.monotonic()
                if not last_fail_logged or now - last_fail_logged[-1] >= 600:
                    last_fail_logged[:] = [now]
                    err = str(exc)

                    def _fail() -> JobResult:
                        raise TelegramError(err)

                    run_job(engine, "telegram_inbound", _fail)
                return
            if n:
                run_job(engine, "telegram_inbound", lambda: JobResult(rows_written=n))

        sched.add_job(telegram_inbound, "interval", seconds=INBOUND_POLL_SECONDS, id="telegram_in")
    sched.add_job(
        wrap("nightly_maintenance", lambda: nightly_maintenance(engine, settings)),
        "cron",
        hour=4,
        minute=0,
        id="nightly_maintenance",
    )
    return sched
