"""Scheduler wiring and jobs. All jobs run in the single worker (`max_instances=1`).

Every job runs through `run_job` (aether/runs.py), which records a `job_runs` row. Network I/O
and LLM calls happen outside write transactions.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import Engine, delete, func, select, update

from aether.alerts.candidates import AlertCandidate
from aether.alerts.dispatch import deliver, enqueue, enqueue_test_alert, run_alerts
from aether.alerts.telegram import TelegramBot, TelegramConfig, TelegramError, TelegramService
from aether.catalysts.mark import CatalystMark, apply_mark
from aether.catalysts.sync import refresh_catalysts
from aether.classify.pipeline import (
    classifier_context,
    has_classifier_work,
    poll_batches,
    run_classify,
)
from aether.config import (
    Settings,
    load_alerts_config,
    load_catalysts_config,
    load_llm_config,
    load_options_config,
    load_rubric,
    load_sources,
    load_strategies,
    load_universe_config,
    load_watchlist,
    load_weights,
)
from aether.db.engine import write_tx
from aether.db.models import commands, conclusions, job_runs
from aether.db.types import to_iso, utcnow_iso
from aether.escalate.run import EscalationDeps, run_escalations
from aether.escalate.select import candidates as escalation_candidates
from aether.facts import load_facts
from aether.ingest.dividends import ingest_dividends
from aether.ingest.earnings_calendar import ingest_earnings_calendar
from aether.ingest.edgar import ingest_edgar
from aether.ingest.fx import ingest_fx
from aether.ingest.news_rss import ingest_news_rss
from aether.ingest.prices import ingest_prices
from aether.ingest.qtum_holdings import ingest_qtum_holdings
from aether.ingest.short_interest import ingest_short_interest
from aether.llm.client import LlmClient, LlmDisabled
from aether.market import last_ok_finished
from aether.ops.backup import backup
from aether.ops.restore import drill
from aether.options.job import snapshot_options
from aether.portfolio.holdings import (
    HoldingsUpdate,
    SettingsUpdate,
    apply_holdings_update,
    apply_settings_update,
)
from aether.portfolio.job import config_changed, run_strategies
from aether.portfolio.publish import publish_targets, run_rebalance, sgt_today
from aether.portfolio.tiger_sync import sync_holdings
from aether.providers.dividends import FallbackDividends, MassiveDividends, YFinanceDividends
from aether.providers.edgar import EdgarClient
from aether.providers.finra import FinraShortInterest
from aether.providers.fx import EcbFx, FallbackFx, YFinanceFx
from aether.providers.options import YFinanceOptions
from aether.providers.prices import (
    US_EASTERN,
    FailoverPriceProvider,
    MassiveProvider,
    YFinanceProvider,
    yfinance_shares,
)
from aether.providers.rss import RssClient
from aether.providers.tiger import TigerConfig, TigerReadOnly
from aether.research.runner import (
    VerifyTarget,
    backfill_state,
    poll_backfill,
    run_sweep,
    run_verify,
    submit_backfill,
)
from aether.review.pack import monthly_review
from aether.runs import JobResult, run_job
from aether.score.calibration import has_report, run_calibration
from aether.score.reaction import run_reactions
from aether.score.scorecard import run_scorecards
from aether.score.theme import run_theme
from aether.score.track_record import run_track_record
from aether.synthesize.brief import weekly_brief
from aether.synthesize.run import SynthDeps, run_conclusions, synth_symbols, synthesize_one
from aether.universe.run import UniverseDeps, run_universe_review

__all__ = ["JobResult", "build_scheduler", "process_commands", "run_job"]

log = logging.getLogger(__name__)

TZ = "Asia/Singapore"
HEARTBEAT_MINUTES = 10
ALERTS_MINUTES = 10
CATALYSTS_MINUTES = 30
INBOUND_POLL_SECONDS = 30
ESCALATION_MINUTES = 2  # safety net; the classifier also triggers a pass after each run
SYNTH_LOCK_WAIT_S = 600  # an escalation waits this long for a running conclusions job


def heartbeat() -> JobResult:
    return JobResult()


CommandHandler = Callable[[dict[str, Any]], dict[str, Any]]

# Handlers for dashboard-requested commands. Each returns a JSON-serialisable result.
# Handlers that need the engine/providers are added in `build_scheduler`. `process_commands`
# adds the command's id to the args as `_command_id` (for holdings_history).
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
        # Mark it running first (short write; the handler runs outside any transaction) so
        # the dashboard can show progress. Skip it if another processor already claimed it.
        with write_tx(engine) as conn:
            claimed = conn.execute(
                update(commands)
                .where(commands.c.id == cmd_id, commands.c.status == "pending")
                .values(status="running")
            ).rowcount
        if not claimed:
            continue
        handler = handlers.get(kind)
        if handler is None:
            status, result = "rejected", {"error": f"unknown command kind {kind!r}"}
        else:
            try:
                status, result = "done", handler({**json.loads(args), "_command_id": cmd_id})
            except Exception as exc:
                log.exception("command %s (%s) failed", cmd_id, kind)
                status, result = "failed", {"error": repr(exc)[:500]}
        with write_tx(engine) as conn:
            conn.execute(
                update(commands)
                .where(commands.c.id == cmd_id, commands.c.status == "running")
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


def restore_drill_job(settings: Settings) -> JobResult:
    """M11: restore the newest backup into a temp dir and render the dashboard from it."""
    result = drill(settings)
    return JobResult(rows_written=len(result.pages), warning=None, provider=result.backup)


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


def rebalance_job(engine: Engine, settings: Settings) -> JobResult:
    return run_rebalance(
        engine, load_strategies(settings.config_dir), weights=load_weights(settings.config_dir)
    )


def track_record_job(engine: Engine, settings: Settings) -> JobResult:
    return run_track_record(engine, load_weights(settings.config_dir).track_record)


def synth_deps(settings: Settings, llm: LlmClient) -> SynthDeps:
    w = load_weights(settings.config_dir)
    return SynthDeps(
        llm=llm,
        llm_cfg=load_llm_config(settings.config_dir),
        model=settings.synth_model,
        conclusions=w.conclusions,
        track=w.track_record,
        facts=load_facts(settings.config_dir),
    )


def conclusions_job(
    engine: Engine, settings: Settings, llm: LlmClient | None, symbols: list[str] | None = None
) -> JobResult:
    if llm is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; conclusions are disabled")
    return run_conclusions(
        engine,
        synth_deps(settings, llm),
        sgt_today(),
        symbols=symbols,
        include_theme=symbols is None,
    )


def brief_job(engine: Engine, settings: Settings, telegram: bool) -> JobResult:
    rubric = load_rubric(settings.config_dir)
    return weekly_brief(
        engine,
        load_weights(settings.config_dir).track_record,
        rubric.risk_flags,
        today=sgt_today(),
        telegram=telegram,
        short_rule=rubric.short_interest,
    )


def latest_conclusion_at(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(select(func.max(conclusions.c.created_at))).scalar()


def make_tiger(settings: Settings) -> tuple[TigerReadOnly | None, str | None]:
    """The read-only Tiger client, or (None, reason) when not configured (fail closed)."""
    config, reason = TigerConfig.from_settings(settings)
    if config is None:
        log.info("tiger sync disabled: %s; holdings stay manual", reason)
        return None, reason
    try:
        return TigerReadOnly.connect(config), None
    except Exception as exc:  # SDK construction error: disable, never log the key
        reason = f"Tiger client setup failed: {type(exc).__name__}"
        log.error("tiger sync disabled: %s", reason)
        return None, reason


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
    llm_cfg = load_llm_config(settings.config_dir)
    return run_alerts(
        engine,
        load_alerts_config(settings.config_dir),
        load_rubric(settings.config_dir).risk_flags,
        service,
        reason,
        off_cycle_min_materiality=load_strategies(
            settings.config_dir
        ).publish.off_cycle_min_materiality,
        llm_budget_alert=(settings.daily_llm_budget_usd, llm_cfg.budget_alert_fraction),
    )


def fx_job(engine: Engine) -> JobResult:
    with httpx.Client(timeout=30) as client:
        return ingest_fx(engine, FallbackFx(YFinanceFx(), EcbFx(client)))


def options_job(engine: Engine, settings: Settings) -> JobResult:
    return snapshot_options(engine, YFinanceOptions(), load_options_config(settings.config_dir))


def reactions_job(engine: Engine, settings: Settings) -> JobResult:
    return run_reactions(engine, load_weights(settings.config_dir).reactions)


def scorecards_job(engine: Engine, settings: Settings) -> JobResult:
    return run_scorecards(
        engine, load_weights(settings.config_dir), load_rubric(settings.config_dir)
    )


def theme_job(engine: Engine) -> JobResult:
    return run_theme(engine)


def calibration_job(engine: Engine, settings: Settings) -> JobResult:
    return run_calibration(engine, load_weights(settings.config_dir).calibration, sgt_today())


def news_rss_job(engine: Engine, settings: Settings) -> JobResult:
    client = RssClient()
    try:
        return ingest_news_rss(
            engine,
            client,
            load_sources(settings.config_dir),
            load_watchlist(settings.config_dir),
        )
    finally:
        client.close()


def make_llm(engine: Engine, settings: Settings) -> tuple[LlmClient | None, str | None]:
    """The LLM wrapper, or (None, reason) when no API key is set (research stays off)."""
    try:
        return LlmClient(engine, settings, load_llm_config(settings.config_dir)), None
    except LlmDisabled as exc:
        log.info("research disabled: %s", exc)
        return None, str(exc)


def research_sweep_job(engine: Engine, settings: Settings, llm: LlmClient | None) -> JobResult:
    if llm is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; research is disabled")
    return run_sweep(
        engine,
        llm,
        load_llm_config(settings.config_dir),
        load_sources(settings.config_dir),
        load_watchlist(settings.config_dir),
        settings.research_model,
    )


def universe_review_job(
    engine: Engine,
    settings: Settings,
    llm: LlmClient | None,
    notify: Callable[[Sequence[AlertCandidate]], None],
    kind: str = "monthly",
) -> JobResult:
    """M12 monthly universe review (spec §6.7). Needs the API key and SEC_USER_AGENT."""
    if llm is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; the universe review is disabled")
    if settings.sec_user_agent is None:
        raise RuntimeError("SEC_USER_AGENT is not set; the universe review is disabled")
    edgar = EdgarClient(settings.sec_user_agent)
    try:
        deps = UniverseDeps(
            llm=llm,
            edgar=edgar,
            # Its own provider instance: the review runs long and mustn't hold the prices lock.
            prices=make_price_provider(settings),
            cfg=load_universe_config(settings.config_dir),
            sources=load_sources(settings.config_dir),
            watchlist=load_watchlist(settings.config_dir),
            overlay=load_strategies(settings.config_dir).overlay,
            model=settings.research_deep_model,
            budget_usd=settings.universe_review_budget_usd,
            notify=notify,
            shares_fallback=yfinance_shares,
        )
        return run_universe_review(engine, deps, today=sgt_today(), kind=kind)
    finally:
        edgar.close()


NEWS_RSS_MINUTES = 60
CLASSIFY_MINUTES = 10
BACKFILL_POLL_MINUTES = 15
BACKFILL_SUBMIT_DELAY = timedelta(minutes=2)
CATCH_UP_AFTER = timedelta(hours=24)
PORTFOLIO_CATCH_UP_DELAY = timedelta(minutes=5)
# M9 scores catch up after prices, EDGAR and the classifier have had a first pass.
SCORES_CATCH_UP_DELAY = timedelta(minutes=8)
CALIBRATION_FIRST_DELAY = timedelta(minutes=12)
# M10: synthesis catches up once at startup when no conclusion is newer than this (owner decision
# 2026-10-06), after the scores have had their first pass.
SYNTH_CATCH_UP_AFTER = timedelta(days=8)
SYNTH_CATCH_UP_DELAY = timedelta(minutes=15)


def _catch_up(engine: Engine, job: str, delay: timedelta = timedelta(0)) -> dict[str, Any]:
    """Extra `add_job` kwargs: run now (+ `delay`) if the last ok run is older than a day.
    Otherwise none, and the cron trigger decides. (APScheduler 3: `next_run_time=None` would
    mean *paused*.)"""
    last = last_ok_finished(engine, job)
    if last is None or datetime.now(UTC) - datetime.fromisoformat(last) > CATCH_UP_AFTER:
        return {"next_run_time": datetime.now(ZoneInfo(TZ)) + delay}
    return {}


def strategies_catch_up(engine: Engine, settings: Settings) -> dict[str, Any]:
    """`_catch_up` for the backtest, plus a rerun whenever `strategies.yaml` changed the backtest
    config since the latest run, however recent that run is (issue #13). `run_rebalance` then
    republishes the targets with trigger `config_change`."""
    if config_changed(engine, load_strategies(settings.config_dir)):
        return {"next_run_time": datetime.now(ZoneInfo(TZ)) + PORTFOLIO_CATCH_UP_DELAY}
    return _catch_up(engine, "strategies", PORTFOLIO_CATCH_UP_DELAY)


def short_interest_job(engine: Engine, settings: Settings) -> JobResult:
    return ingest_short_interest(
        engine, FinraShortInterest(), load_rubric(settings.config_dir).short_interest
    )


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
        run_catalysts()

    def refresh_edgar(_args: dict[str, Any]) -> dict[str, Any]:
        if not edgar_lock.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            result = run_job(engine, "edgar", lambda: edgar_job(engine, settings))
        finally:
            edgar_lock.release()
        run_catalysts()
        return {"ok": result is not None, "rows": result.rows_written if result else 0}

    # Dividends then backtests (M4), daily 07:10 SGT after prices. The command shares the lock.
    dividend_provider = make_dividend_provider(settings)
    portfolio_lock = threading.Lock()

    def run_rebalance_step() -> JobResult | None:
        return run_job(engine, "rebalance", lambda: rebalance_job(engine, settings))

    def portfolio_pipeline() -> JobResult | None:
        run_job(engine, "dividends", lambda: ingest_dividends(engine, dividend_provider))
        # Backtests run even if the dividend fetch failed: stored dividends are still valid.
        result = run_job(engine, "strategies", lambda: strategies_job(engine, settings))
        # Published targets + plan (M5) follow the backtest (they use whatever run is latest).
        run_rebalance_step()
        return result

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

    def replan() -> dict[str, Any]:
        """Re-run targets + plan after an owner change; waits for a running pipeline."""
        with portfolio_lock:
            result = run_rebalance_step()
        return {"replanned": result is not None}

    def update_holdings(args: dict[str, Any]) -> dict[str, Any]:
        command_id = args.pop("_command_id", None)
        out = apply_holdings_update(engine, HoldingsUpdate.model_validate(args), command_id)
        return {**out, **replan()}

    def publish_now(args: dict[str, Any]) -> dict[str, Any]:
        """Publish targets now (off-cycle). The owner's decision; cites the event if given."""
        args.pop("_command_id", None)
        eid = args.get("trigger_event_id")
        eid = eid if isinstance(eid, int) and not isinstance(eid, bool) and eid > 0 else None
        with portfolio_lock:
            result = run_job(
                engine,
                "publish_targets",
                lambda: publish_targets(
                    engine,
                    load_strategies(settings.config_dir),
                    trigger="off_cycle",
                    trigger_event_id=eid,
                    weights=load_weights(settings.config_dir),
                ),
            )
        return {"ok": result is not None, "rows": result.rows_written if result else 0}

    def update_portfolio_settings(args: dict[str, Any]) -> dict[str, Any]:
        args.pop("_command_id", None)
        out = apply_settings_update(engine, SettingsUpdate.model_validate(args))
        return {**out, **replan()}

    # Tiger holdings sync (S8, M5): daily 07:05 SGT when configured, and on demand.
    tiger, tiger_off = make_tiger(settings)

    def tiger_sync_once(command_id: int | None) -> JobResult | None:
        return run_job(
            engine, "tiger_sync", lambda: sync_holdings(engine, tiger, tiger_off, command_id)
        )

    def run_tiger_sync() -> None:
        if tiger_sync_once(None) is not None:
            replan()

    def sync_holdings_command(args: dict[str, Any]) -> dict[str, Any]:
        result = tiger_sync_once(args.pop("_command_id", None))
        if result is None:
            return {"ok": False, "error": "sync failed; last snapshot kept (see Holdings page)"}
        return {"ok": True, "rows": result.rows_written, **replan()}

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

    # News & research (M6). RSS needs no key; research runs only with ANTHROPIC_API_KEY set.
    llm, llm_off = make_llm(engine, settings)
    research_lock = threading.Lock()

    # Classifier (M7): rules → LLM (no tools) → caps. Runs every 10 minutes and right after each
    # news/research ingest; a job_runs row only when something waits (no idle noise).
    classify_lock = threading.Lock()

    def classify_pass() -> None:
        ctx = classifier_context(settings)
        if llm is not None and has_classifier_work(engine, batches_only=True):
            run_job(
                engine,
                "classify_batch_poll",
                lambda: poll_batches(engine, llm, ctx) or JobResult(warning="nothing open"),
            )
        if has_classifier_work(engine):
            run_job(engine, "classify", lambda: run_classify(engine, llm, ctx))

    def run_classifier() -> None:
        if not classify_lock.acquire(blocking=False):
            return  # another run is in progress; it will pick the new items up
        try:
            classify_pass()
        finally:
            classify_lock.release()
        run_catalysts()
        run_escalations_job()  # M11: escalate right after classification

    def run_news_rss() -> None:
        run_job(engine, "news_rss", lambda: news_rss_job(engine, settings))
        run_classifier()

    def run_research_sweep() -> None:
        if llm is None:
            return  # disabled: logged once at startup, no job_runs noise
        with research_lock:
            run_job(engine, "research_sweep", lambda: research_sweep_job(engine, settings, llm))
        run_classifier()

    def research_sweep_command(_args: dict[str, Any]) -> dict[str, Any]:
        if llm is None:
            return {"ok": False, "error": llm_off}
        if not research_lock.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            result = run_job(
                engine, "research_sweep", lambda: research_sweep_job(engine, settings, llm)
            )
        finally:
            research_lock.release()
        run_classifier()
        return {"ok": result is not None, "rows": result.rows_written if result else 0}

    def run_backfill_submit() -> None:
        # Owner decision (2026-10-05): the 12-month backfill runs automatically, once, no cap.
        if llm is None or not settings.research_backfill:
            return
        if backfill_state(engine, datetime.now(UTC)) != "none":
            return
        with research_lock:
            run_job(
                engine,
                "research_backfill",
                lambda: submit_backfill(
                    engine,
                    llm,
                    load_llm_config(settings.config_dir),
                    load_sources(settings.config_dir),
                    load_watchlist(settings.config_dir),
                    settings.research_model,
                ),
            )

    def run_backfill_poll() -> None:
        if llm is None or backfill_state(engine, datetime.now(UTC)) != "open":
            return
        with research_lock:
            run_job(
                engine,
                "research_backfill_poll",
                lambda: (
                    poll_backfill(engine, llm, load_sources(settings.config_dir))
                    or JobResult(warning="nothing open")
                ),
            )
        run_classifier()

    # Catalysts (M8): DB-only sync + deterministic resolution. Every 30 minutes and after the
    # earnings, EDGAR and classifier runs; a job_runs row only when something changed (or failed).
    catalysts_lock = threading.Lock()

    def run_catalysts() -> None:
        with catalysts_lock:
            try:
                cfg = load_catalysts_config(settings.config_dir)
                summary = refresh_catalysts(engine, cfg, datetime.now(US_EASTERN).date())
            except Exception as exc:
                err = repr(exc)[:500]
                log.exception("catalysts refresh failed")

                def _fail() -> JobResult:
                    raise RuntimeError(err)

                run_job(engine, "catalysts", _fail)
                return
            if summary.changed:
                run_job(engine, "catalysts", lambda: JobResult(rows_written=summary.changed))

    # Conclusions (M10): Sunday 08:30 SGT, after the calibration report; the `synthesize` command
    # re-runs one ticker (or all + theme). Budget-guarded synchronous calls, no tools.
    synth_lock = threading.Lock()

    def synthesize_command(args: dict[str, Any]) -> dict[str, Any]:
        args.pop("_command_id", None)
        if llm is None:
            return {"ok": False, "error": llm_off}
        sym = args.get("symbol")
        known = synth_symbols(engine)
        if sym is not None and sym not in known:
            return {"ok": False, "error": f"unknown symbol {sym!r}"}
        if not synth_lock.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            result = run_job(
                engine,
                "conclusions",
                lambda: conclusions_job(engine, settings, llm, [sym] if sym else None),
            )
        finally:
            synth_lock.release()
        return {"ok": result is not None, "rows": result.rows_written if result else 0}

    def run_conclusions_job() -> None:
        if llm is None:
            return  # disabled: logged once at startup
        with synth_lock:
            run_job(engine, "conclusions", lambda: conclusions_job(engine, settings, llm))

    # Escalation (M11, spec §5.2.5): alert → verification research → re-synthesis, with caps.
    # Runs after every classifier pass and every 2 minutes; a job_runs row only when a candidate
    # exists. The non-blocking lock also stops the classify step inside an escalation recursing.
    escalation_lock = threading.Lock()

    def notify(cands: Sequence[AlertCandidate]) -> None:
        on = telegram is not None and telegram.blocked is None
        now = datetime.now(UTC)
        enqueue(engine, cands, telegram=on, now=now)
        if on and telegram is not None:
            with alerts_lock:
                deliver(engine, telegram, load_alerts_config(settings.config_dir), now)

    def verify(target: VerifyTarget) -> tuple[int, int]:
        assert llm is not None
        cfg = load_llm_config(settings.config_dir)
        return run_verify(
            engine,
            llm,
            cfg,
            load_sources(settings.config_dir),
            settings.research_model,
            target,
            max_uses=cfg.escalation.verify_max_uses,
            window_days=cfg.escalation.verify_window_days,
        )

    def classify_for_escalation() -> None:
        with classify_lock:
            classify_pass()

    def resynthesize(symbol: str) -> dict[str, Any]:
        assert llm is not None
        if not synth_lock.acquire(timeout=SYNTH_LOCK_WAIT_S):
            return {"status": "failed", "error": "a conclusions run is still in progress"}
        try:
            return synthesize_one(engine, synth_deps(settings, llm), "ticker", symbol, sgt_today())
        finally:
            synth_lock.release()

    def escalation_deps() -> EscalationDeps:
        watchlist = load_watchlist(settings.config_dir)
        return EscalationDeps(
            params=load_llm_config(settings.config_dir).escalation,
            max_per_day=settings.max_escalations_per_day,
            lookback_days=load_alerts_config(settings.config_dir).event_lookback_days,
            symbols=frozenset(synth_symbols(engine)),
            names={t.symbol: (t.aliases[0] if t.aliases else t.symbol) for t in watchlist.tickers},
            notify=notify,
            verify=verify if llm is not None else None,
            classify=classify_for_escalation,
            resynthesize=resynthesize if llm is not None else None,
            disabled_reason=llm_off,
        )

    def run_escalations_job() -> None:
        if not escalation_lock.acquire(blocking=False):
            return
        try:
            deps = escalation_deps()
            if not escalation_candidates(
                engine, deps.params, deps.symbols, datetime.now(UTC), deps.lookback_days
            ):
                return
            run_job(engine, "escalations", lambda: run_escalations(engine, deps))
        finally:
            escalation_lock.release()

    def mark_catalyst(args: dict[str, Any]) -> dict[str, Any]:
        args.pop("_command_id", None)
        with catalysts_lock:
            return apply_mark(engine, CatalystMark.model_validate(args))

    # Monthly universe review (M12, spec §6.7): the 1st at 10:00 SGT; the 2nd retries a failed
    # month (it does nothing once the month is done). The `universe_review` command runs one now.
    universe_lock = threading.Lock()

    def run_universe_review_job() -> None:
        if llm is None:
            return  # disabled: logged once at startup
        with universe_lock:
            run_job(
                engine,
                "universe_review",
                lambda: universe_review_job(engine, settings, llm, notify),
            )

    def universe_review_command(_args: dict[str, Any]) -> dict[str, Any]:
        if llm is None:
            return {"ok": False, "error": llm_off}
        if not universe_lock.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            result = run_job(
                engine,
                "universe_review",
                lambda: universe_review_job(engine, settings, llm, notify, kind="manual"),
            )
        finally:
            universe_lock.release()
        if result is None:
            return {"ok": False, "error": "the review failed; see /universe"}
        return {"ok": True, "rows": result.rows_written}

    handlers = {
        **COMMAND_HANDLERS,
        "universe_review": universe_review_command,
        "mark_catalyst": mark_catalyst,
        "research_sweep": research_sweep_command,
        "refresh_prices": refresh_prices,
        "refresh_edgar": refresh_edgar,
        "test_alert": test_alert,
        "recompute_strategies": recompute_strategies,
        "update_holdings": update_holdings,
        "update_portfolio_settings": update_portfolio_settings,
        "sync_holdings": sync_holdings_command,
        "publish_targets": publish_now,
        "synthesize": synthesize_command,
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

    def run_earnings_calendar() -> None:
        run_job(engine, "earnings_calendar", lambda: earnings_calendar_job(engine))
        run_catalysts()

    sched.add_job(
        run_earnings_calendar,
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
        **strategies_catch_up(engine, settings),
    )
    if tiger is not None:
        sched.add_job(run_tiger_sync, "cron", hour=7, minute=5, id="tiger_sync")
    # Catalysts (M8): every 30 min, first run shortly after start.
    sched.add_job(
        run_catalysts,
        "interval",
        minutes=CATALYSTS_MINUTES,
        id="catalysts",
        next_run_time=datetime.now(ZoneInfo(TZ)) + timedelta(seconds=30),
    )
    # FINRA short interest (M8): daily check 07:20 SGT; new files arrive about twice a month.
    sched.add_job(
        wrap("short_interest", lambda: short_interest_job(engine, settings)),
        "cron",
        hour=7,
        minute=20,
        id="short_interest",
        **_catch_up(engine, "short_interest", PORTFOLIO_CATCH_UP_DELAY),
    )
    # Options snapshot (06:40) and USD/SGD (06:50), M5; research/reporting only.
    sched.add_job(
        wrap("options", lambda: options_job(engine, settings)),
        "cron",
        hour=6,
        minute=40,
        id="options",
        **_catch_up(engine, "options", PORTFOLIO_CATCH_UP_DELAY),
    )
    sched.add_job(
        wrap("fx", lambda: fx_job(engine)),
        "cron",
        hour=6,
        minute=50,
        id="fx",
        **_catch_up(engine, "fx"),
    )

    # M9 scores (spec §9): event reactions 06:45, theme decomposition + scorecards 07:00 (after
    # prices at 06:30), calibration report Sunday 08:00 (and once at startup if none exists).
    sched.add_job(
        wrap("reactions", lambda: reactions_job(engine, settings)),
        "cron",
        hour=6,
        minute=45,
        id="reactions",
        **_catch_up(engine, "reactions", SCORES_CATCH_UP_DELAY),
    )

    def run_scores() -> None:
        run_job(engine, "theme", lambda: theme_job(engine))
        run_job(engine, "scorecards", lambda: scorecards_job(engine, settings))

    sched.add_job(
        run_scores,
        "cron",
        hour=7,
        minute=0,
        id="scores",
        **_catch_up(engine, "scorecards", SCORES_CATCH_UP_DELAY + timedelta(minutes=1)),
    )
    sched.add_job(
        wrap("calibration", lambda: calibration_job(engine, settings)),
        "cron",
        day_of_week="sun",
        hour=8,
        minute=0,
        id="calibration",
        **(
            {}
            if has_report(engine)
            else {"next_run_time": datetime.now(ZoneInfo(TZ)) + CALIBRATION_FIRST_DELAY}
        ),
    )

    # Monthly publish + review pack: the 1st at 10:30 SGT; the 2nd is the retry (it does nothing
    # if the month's pack is already done).
    def run_monthly_review() -> None:
        on = telegram is not None and telegram.blocked is None
        with portfolio_lock:
            run_job(
                engine,
                "review_pack",
                lambda: monthly_review(
                    engine,
                    load_strategies(settings.config_dir),
                    load_rubric(settings.config_dir).risk_flags,
                    today=sgt_today(),
                    telegram=on,
                    short_rule=load_rubric(settings.config_dir).short_interest,
                    weights=load_weights(settings.config_dir),
                ),
            )

    sched.add_job(run_monthly_review, "cron", day="1,2", hour=10, minute=30, id="review_pack")
    sched.add_job(
        run_universe_review_job, "cron", day="1,2", hour=10, minute=0, id="universe_review"
    )

    # M10: track record + overlay outcomes daily 07:30; conclusions Sunday 08:30 (and once,
    # 15 min after start, if no conclusion is newer than 8 days and an API key is set); the
    # weekly brief Sunday 09:00.
    sched.add_job(
        wrap("track_record", lambda: track_record_job(engine, settings)),
        "cron",
        hour=7,
        minute=30,
        id="track_record",
        **_catch_up(engine, "track_record", SCORES_CATCH_UP_DELAY + timedelta(minutes=2)),
    )
    last_synth = latest_conclusion_at(engine)
    synth_due = (
        last_synth is None
        or datetime.now(UTC) - datetime.fromisoformat(last_synth) > SYNTH_CATCH_UP_AFTER
    )
    sched.add_job(
        run_conclusions_job,
        "cron",
        day_of_week="sun",
        hour=8,
        minute=30,
        id="conclusions",
        **(
            {"next_run_time": datetime.now(ZoneInfo(TZ)) + SYNTH_CATCH_UP_DELAY}
            if synth_due and llm is not None
            else {}
        ),
    )

    def run_brief() -> None:
        on = telegram is not None and telegram.blocked is None
        run_job(engine, "weekly_brief", lambda: brief_job(engine, settings, on))

    sched.add_job(run_brief, "cron", day_of_week="sun", hour=9, minute=0, id="weekly_brief")
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
    # News & research (M6): RSS hourly; research sweeps 08:00 / 20:00 SGT (spec §9); the backfill
    # is submitted once shortly after start and polled every 15 min while its batch is open.
    sched.add_job(
        run_news_rss,
        "interval",
        minutes=NEWS_RSS_MINUTES,
        id="news_rss",
        next_run_time=datetime.now(ZoneInfo(TZ)) + timedelta(minutes=1),
    )
    sched.add_job(run_classifier, "interval", minutes=CLASSIFY_MINUTES, id="classify")
    sched.add_job(run_escalations_job, "interval", minutes=ESCALATION_MINUTES, id="escalations")
    sched.add_job(run_research_sweep, "cron", hour="8,20", minute=0, id="research_sweep")
    sched.add_job(
        run_backfill_submit,
        "date",
        run_date=datetime.now(ZoneInfo(TZ)) + BACKFILL_SUBMIT_DELAY,
        id="research_backfill_submit",
    )
    sched.add_job(
        run_backfill_poll, "interval", minutes=BACKFILL_POLL_MINUTES, id="research_backfill_poll"
    )
    sched.add_job(
        wrap("nightly_maintenance", lambda: nightly_maintenance(engine, settings)),
        "cron",
        hour=4,
        minute=0,
        id="nightly_maintenance",
    )
    # M11: weekly backup restore drill, after Sunday's backup.
    sched.add_job(
        wrap("restore_drill", lambda: restore_drill_job(settings)),
        "cron",
        day_of_week="sun",
        hour=4,
        minute=30,
        id="restore_drill",
    )
    return sched
