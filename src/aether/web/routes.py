from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from aether import feed_view, market, news_view, sec_view
from aether.alerts import view as alerts_view
from aether.catalysts import view as catalysts_view
from aether.catalysts.mark import CatalystMark
from aether.classify.prompt import prompt_version
from aether.config import (
    CATEGORY_CLASS,
    PROFILES,
    load_alerts_config,
    load_llm_config,
    load_rubric,
    load_strategies,
    load_weights,
)
from aether.db import health
from aether.db.commands import (
    active_command,
    count_recent_commands,
    enqueue_command,
    get_command,
    rate_limit_resets_at,
)
from aether.ops import view as ops_view
from aether.options.view import options_panel, options_stale
from aether.portfolio import holdings_view, performance
from aether.portfolio import view as strategies_view
from aether.portfolio.holdings import (
    HoldingsUpdate,
    PositionIn,
    SettingsUpdate,
    load_settings,
    universe_symbols,
)
from aether.providers.prices import US_EASTERN
from aether.risk.flags import open_flags
from aether.score import view as score_view
from aether.security import auth, csrf
from aether.synthesize import view as synth_view
from aether.universe import view as universe_view
from aether.web import command_view
from aether.web.command_view import CommandStatus

router = APIRouter()

DISCLAIMER = "Personal research tool, not financial advice."
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
TYPE_ORDER = ("etf", "pure_play", "benchmark", "context")
TYPE_LABELS = {
    "etf": "Theme ETF",
    "pure_play": "Pure-plays",
    "benchmark": "Benchmarks",
    "context": "Context",
}


def _render(request: Request, template: str, context: dict[str, object]) -> HTMLResponse:
    token = csrf.token_for(request, request.app.state.csrf)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, template, {"disclaimer": DISCLAIMER, **context, "csrf_token": token}
    )
    csrf.set_cookie(response, token)
    return response


def _today() -> date:
    # Trading-day dates are US/Eastern session dates.
    return datetime.now(US_EASTERN).date()


def _known_symbol(request: Request, symbol: str) -> str:
    if not SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(404)
    if symbol not in dict(market.load_tickers(request.app.state.ro_engine)):
        raise HTTPException(404)
    return symbol


# --------------------------------------------------------------------------- pages


@router.get("/", response_class=HTMLResponse)
def overview(request: Request) -> HTMLResponse:
    engine = request.app.state.ro_engine
    today = _today()
    ticker_types = market.load_tickers(engine)
    closes = market.load_closes(engine, [s for s, _ in ticker_types])
    providers = market.latest_providers(engine)
    summaries = [
        market.summarize(s, t, closes[s], providers.get(s), today) for s, t in ticker_types
    ]
    groups = [
        (TYPE_LABELS[t], [x for x in summaries if x.type == t])
        for t in TYPE_ORDER
        if any(x.type == t for x in summaries)
    ]
    pure = [s for s, t in ticker_types if t == "pure_play"]
    rubric = load_rubric(request.app.state.settings.config_dir)
    return _render(
        request,
        "overview.html",
        {
            "groups": groups,
            "fresh": market.freshness(engine, summaries),
            "holdings": market.holdings_view(engine, pure, today),
            "pure": pure,
            "ranges": list(market.RANGES),
            "default_range": market.DEFAULT_RANGE,
            "flags": open_flags(engine, rubric.risk_flags, today, pure, rubric.short_interest),
            "risk_events": sec_view.recent_events(engine, days=30),
            "sec_fresh": sec_view.sec_freshness(engine),
            "alerts": alerts_view.recent_alerts(engine, limit=5),
            "drift": holdings_view.drift_card(engine),
            "catalysts": catalysts_view.upcoming(engine, today, days=365),
            "scores": score_view.latest_totals(engine),
            "theme": score_view.theme_card(engine),
            "score_fresh": score_view.freshness(engine),
            "stances": _stances(request, today),
        },
    )


def _stances(request: Request, today: date) -> dict[str, dict[str, Any]]:
    """The stance table keyed by symbol (`$THEME` for the theme tilt)."""
    engine = request.app.state.ro_engine
    track = load_weights(request.app.state.settings.config_dir).track_record
    return {r["key"]: r for r in synth_view.stance_table(engine, track, today)}


@router.get("/t/{symbol}", response_class=HTMLResponse)
def ticker_page(request: Request, symbol: str) -> HTMLResponse:
    symbol = _known_symbol(request, symbol)
    engine = request.app.state.ro_engine
    type_ = dict(market.load_tickers(engine))[symbol]
    series = market.load_closes(engine, [symbol])[symbol]
    summary = market.summarize(
        symbol, type_, series, market.latest_providers(engine).get(symbol), _today()
    )
    sec: dict[str, object] = {}
    if type_ == "pure_play":
        rubric = load_rubric(request.app.state.settings.config_dir)
        today = _today()
        sec = {
            "flags": open_flags(engine, rubric.risk_flags, today, [symbol], rubric.short_interest),
            "events": sec_view.recent_events(
                engine, days=365, classes=("RISK", "SIGNAL"), symbol=symbol, limit=20
            ),
            "filings": sec_view.filings_for(engine, symbol),
            "insiders": sec_view.insiders_for(engine, symbol),
            "capital": sec_view.capital_for(engine, symbol),
            "lockups": sec_view.lockups_for(engine, symbol),
            "earnings": sec_view.earnings_for(engine, symbol, today),
            "fresh": sec_view.sec_freshness(engine),
            "today": today.isoformat(),
        }
    today = _today()
    options: dict[str, object] = {}
    if type_ in ("etf", "pure_play"):
        last, stale = options_stale(engine)
        options = {"panel": options_panel(engine, [symbol])[0], "last_ok": last, "stale": stale}
    return _render(
        request,
        "ticker.html",
        {
            "catalysts": catalysts_view.upcoming(engine, today, days=730, symbol=symbol),
            "catalysts_done": catalysts_view.resolved(engine, limit=10, symbol=symbol),
            "short_interest": catalysts_view.short_interest_rows(engine, symbol)
            if type_ in ("etf", "pure_play")
            else [],
            "options": options,
            "s": summary,
            "type_label": TYPE_LABELS[type_],
            "n_bars": len(series),
            "sec": sec,
            "code_labels": sec_view.CODE_LABELS,
            "news": news_view.news_rows(engine, symbol=symbol, limit=10)
            if type_ in ("etf", "pure_play")
            else [],
            "origin_labels": news_view.ORIGIN_LABELS,
            "score": score_view.latest_scorecard(engine, symbol)
            if type_ in ("etf", "pure_play")
            else None,
            "reactions": score_view.reactions_for(engine, symbol)
            if type_ in ("etf", "pure_play")
            else [],
            "score_fresh": score_view.freshness(engine),
            "fd_labels": score_view.FD_LABELS,
            "conclusion": synth_view.ticker_conclusions(engine, symbol)
            if type_ in ("etf", "pure_play")
            else None,
            "stance": _stances(request, today).get(symbol),
            "cite": synth_view.citation,
        },
    )


@router.get("/track-record", response_class=HTMLResponse)
def track_record_page(request: Request) -> HTMLResponse:
    engine = request.app.state.ro_engine
    track = load_weights(request.app.state.settings.config_dir).track_record
    return _render(
        request,
        "track_record.html",
        {
            "tr": synth_view.track_record_page(engine, track),
            "stances": synth_view.stance_table(engine, track, _today()),
            "theme": synth_view.ticker_conclusions(engine, None, limit=10),
            "cite": synth_view.citation,
            "min_calls": track.min_mature_calls,
        },
    )


@router.get("/briefs", response_class=HTMLResponse)
def briefs_page(request: Request) -> HTMLResponse:
    return _render(
        request, "briefs.html", {"briefs": synth_view.briefs_view(request.app.state.ro_engine)}
    )


@router.get("/calibration", response_class=HTMLResponse)
def calibration_page(request: Request) -> HTMLResponse:
    engine = request.app.state.ro_engine
    return _render(
        request,
        "calibration.html",
        {"c": score_view.calibration_view(engine), "fresh": score_view.freshness(engine)},
    )


@router.get("/catalysts", response_class=HTMLResponse)
def catalysts_page(request: Request) -> HTMLResponse:
    engine = request.app.state.ro_engine
    today = _today()
    return _render(
        request,
        "catalysts.html",
        {
            "upcoming": catalysts_view.upcoming(engine, today, days=3650),
            "resolved": catalysts_view.resolved(engine, limit=200),
            "counts": catalysts_view.hit_slip_counts(engine),
            "today": today.isoformat(),
        },
    )


@router.get("/news", response_class=HTMLResponse)
def news_page(request: Request, symbol: str = "", origin: str = "") -> HTMLResponse:
    engine = request.app.state.ro_engine
    known = [s for s, t in market.load_tickers(engine) if t in ("etf", "pure_play")]
    sym = symbol if symbol in known else None
    org = origin if origin in news_view.NEWS_ORIGINS else None
    return _render(
        request,
        "news.html",
        {
            "rows": news_view.news_rows(engine, symbol=sym, origin=org),
            "symbols": known,
            "symbol": sym or "",
            "origin": org or "",
            "origin_labels": news_view.ORIGIN_LABELS,
            "spend": news_view.llm_spend(engine, request.app.state.settings.daily_llm_budget_usd),
            "runs": news_view.research_runs_rows(engine),
            "backfill": news_view.backfill_summary(engine),
            "feeds": news_view.feeds(engine),
            "stale": news_view.rss_stale(engine),
        },
    )


@router.get("/feed", response_class=HTMLResponse)
def feed_page(
    request: Request,
    cls: str = "",
    category: str = "",
    symbol: str = "",
    min_materiality: str = "1",
    tier: str = "",
    noise: str = "",
    event: str = "",
) -> HTMLResponse:
    engine = request.app.state.ro_engine
    known = [s for s, t in market.load_tickers(engine) if t in ("etf", "pure_play")]
    f = feed_view.FeedFilters.parse(
        event=event,
        klass=cls,
        category=category,
        symbol=symbol,
        min_materiality=min_materiality,
        tier=tier,
        noise=noise,
        symbols=known,
    )
    version = prompt_version(load_rubric(request.app.state.settings.config_dir).classifier)
    return _render(
        request,
        "feed.html",
        {
            "rows": feed_view.feed_rows(engine, f),
            "f": f,
            "counts": feed_view.feed_counts(engine),
            "failed": feed_view.failed_rows(engine),
            "symbols": known,
            "classes": feed_view.CLASSES,
            "categories": sorted(CATEGORY_CLASS),
            "tiers": feed_view.TIERS,
            "origin_labels": feed_view.ORIGIN_LABELS,
            "prompt_version": version,
            "classifier_model": request.app.state.settings.classifier_model,
            "eval": feed_view.latest_eval(engine, version),
        },
    )


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request) -> HTMLResponse:
    engine = request.app.state.ro_engine
    cfg = load_alerts_config(request.app.state.settings.config_dir)
    return _render(
        request,
        "alerts.html",
        {
            "alerts": alerts_view.recent_alerts(engine, limit=100),
            "delivery": alerts_view.delivery_status(engine),
            "immediate_min": cfg.immediate_min_materiality,
            "digest_time": cfg.digest_time,
        },
    )


@router.get("/facts", response_class=HTMLResponse)
def facts_page(request: Request) -> HTMLResponse:
    rows = alerts_view.facts_rows(request.app.state.ro_engine)
    counts = {k: sum(1 for f in rows if f.status == k) for k in alerts_view.STATUS_LABELS}
    return _render(
        request,
        "facts.html",
        {"facts": rows, "counts": counts, "labels": alerts_view.STATUS_LABELS},
    )


@router.get("/strategies", response_class=HTMLResponse)
def strategies_page(request: Request) -> HTMLResponse:
    engine = request.app.state.ro_engine
    return _render(
        request,
        "strategies.html",
        {
            "v": strategies_view.strategies_view(engine),
            "fresh": strategies_view.freshness(engine),
            "banner": strategies_view.BANNER,
            "family_labels": strategies_view.FAMILY_LABELS,
            "columns": strategies_view.METRIC_COLUMNS,
            "disclaimer": f"{DISCLAIMER} {strategies_view.BANNER}",
        },
    )


@router.get("/holdings", response_class=HTMLResponse)
def holdings_page(request: Request) -> HTMLResponse:
    engine = request.app.state.ro_engine
    h = holdings_view.holdings_page(engine, _today())
    return _render(
        request,
        "holdings.html",
        {
            "h": h,
            "perf_available": not h.holdings.empty or performance.has_history(engine),
            "perf_ranges": [*market.RANGES, performance.SINCE],
            "overlay_value": synth_view.overlay_line(engine),
            "profiles": PROFILES,
            "banner": strategies_view.BANNER,
            "family_labels": strategies_view.FAMILY_LABELS,
            "disclaimer": f"{DISCLAIMER} {strategies_view.BANNER}",
        },
    )


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request) -> HTMLResponse:
    return _render(
        request,
        "review.html",
        {
            "packs": holdings_view.review_packs_view(request.app.state.ro_engine),
            "disclaimer": f"{DISCLAIMER} {strategies_view.BANNER}",
            "banner": strategies_view.BANNER,
        },
    )


@router.get("/universe", response_class=HTMLResponse)
def universe_page(request: Request, review: int | None = None) -> HTMLResponse:
    """M12: the latest universe review (or `?review=<id>`) and the run history."""
    state = request.app.state
    engine = state.ro_engine
    history = universe_view.reviews(engine)
    shown = next((r for r in history if r.id == review), None) if review else None
    if shown is None:
        shown = next((r for r in history if r.status == "done"), None)
    return _render(
        request,
        "universe.html",
        {
            "history": history,
            "review": shown,
            "rows": universe_view.candidates(engine, shown.id) if shown else [],
            "sweep": universe_view.sweep_evidence(engine, shown.id) if shown else {},
            "budget": state.settings.universe_review_budget_usd,
            "model": state.settings.research_deep_model,
        },
    )


# --------------------------------------------------------------------------- login (S2, M5)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> Response:
    target = auth.safe_next(next)
    if request.app.state.auth.session_valid(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse(target, status_code=303)
    return _render(request, "login.html", {"next": target})


@router.post("/login")
async def login_submit(request: Request) -> Response:
    state = request.app.state
    ip = _client_ip(request)
    limiter: auth.LoginLimiter = state.login_limiter
    if limiter.blocked(ip):
        auth.log.warning("login blocked (rate limit) from %s", ip)
        return HTMLResponse("Too many failed attempts. Try again in 15 minutes.", status_code=429)
    form = await request.form()
    password = form.get("password")
    target = auth.safe_next(str(form.get("next") or "/"))
    if isinstance(password, str) and password and state.auth.password.verify(password):
        limiter.reset(ip)
        response = Response(status_code=204, headers={"HX-Redirect": target})
        auth.set_session_cookie(response, state.auth.new_session())
        return response
    n = limiter.record_failure(ip)
    auth.log.warning("failed login from %s (%d in window)", ip, n)  # never the password
    return HTMLResponse("Wrong password.", status_code=401)


@router.post("/logout")
def logout(request: Request) -> Response:
    response = Response(status_code=204, headers={"HX-Redirect": "/login"})
    auth.clear_session_cookie(response)
    return response


@router.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request) -> HTMLResponse:
    state = request.app.state
    engine, settings = state.ro_engine, state.settings
    hours = load_alerts_config(settings.config_dir).job_failing_hours
    return _render(
        request,
        "ops.html",
        {
            "jobs": health.collect(engine, settings.db_path).jobs,
            "failing": ops_view.failing(engine, hours),
            "failing_hours": hours,
            "spend": news_view.llm_spend(engine, settings.daily_llm_budget_usd),
            "by_purpose": ops_view.spend_by_purpose(engine),
            "spend_days": ops_view.SPEND_DAYS,
            "esc": ops_view.escalation_summary(
                engine,
                settings.max_escalations_per_day,
                budget=settings.escalation_daily_budget_usd,
            ),
            "messages": ops_view.message_counts(engine),
            "esc_params": load_llm_config(settings.config_dir).escalation,
            "evals": ops_view.eval_scores(engine),
            "backups": ops_view.backups(engine, settings.resolved_backup_dir),
        },
    )


@router.get("/health", response_class=HTMLResponse)
def health_page(request: Request) -> HTMLResponse:
    state = request.app.state
    h = health.collect(state.ro_engine, state.settings.db_path)
    return _render(request, "health.html", {"h": h, "db_path": str(state.settings.db_path)})


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    state = request.app.state
    h = health.collect(state.ro_engine, state.settings.db_path)
    body = asdict(h)
    body.pop("jobs")
    return JSONResponse(body, status_code=200 if h.ok else 503)


# --------------------------------------------------------------------------- chart data (JSON)


@router.get("/api/prices/overview")
def api_overview(request: Request, range: str = market.DEFAULT_RANGE) -> JSONResponse:
    if range not in market.RANGES:
        raise HTTPException(400, "unknown range")
    series = market.overview_series(request.app.state.ro_engine, range, _today())
    return JSONResponse(
        {
            "range": range,
            "series": [{"name": name, "data": data} for name, data in series.items()],
        }
    )


@router.get("/api/prices/{symbol}")
def api_ticker(request: Request, symbol: str) -> JSONResponse:
    symbol = _known_symbol(request, symbol)
    return JSONResponse(
        {"symbol": symbol, "bars": market.load_ohlcv(request.app.state.ro_engine, symbol)}
    )


@router.get("/api/dilution/{symbol}")
def api_dilution(request: Request, symbol: str) -> JSONResponse:
    symbol = _known_symbol(request, symbol)
    series = sec_view.dilution_series(request.app.state.ro_engine, symbol)
    return JSONResponse(
        {
            "symbol": symbol,
            "series": [{"name": name, "data": data} for name, data in series.items()],
        }
    )


@router.get("/api/reactions/{symbol}")
def api_reactions(request: Request, symbol: str) -> JSONResponse:
    symbol = _known_symbol(request, symbol)
    return JSONResponse(
        {"symbol": symbol, "markers": score_view.markers(request.app.state.ro_engine, symbol)}
    )


@router.get("/api/catalysts")
def api_catalysts(request: Request) -> JSONResponse:
    engine = request.app.state.ro_engine
    today = _today()
    return JSONResponse(
        {"today": today.isoformat(), "items": catalysts_view.timeline(engine, today, days=365)}
    )


@router.get("/api/strategies/curves")
def api_strategy_curves(request: Request, profile: str = "safe") -> JSONResponse:
    if profile not in PROFILES:
        raise HTTPException(404)
    return JSONResponse(strategies_view.curves_for(request.app.state.ro_engine, profile))


@router.get("/api/holdings/performance")
def api_holdings_performance(
    request: Request, range: str = market.DEFAULT_RANGE, mode: str = "actual"
) -> JSONResponse:
    """Sleeve vs QTUM vs QQQ (issue #24): index levels and percentages only."""
    if range not in performance.RANGE_KEYS or mode not in performance.MODES:
        raise HTTPException(400, "unknown range or mode")
    state = request.app.state
    ann = load_strategies(state.settings.config_dir).backtest.annualization
    body = performance.performance(state.ro_engine, range, mode, _today(), ann)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- commands


def _command_status(request: Request, status: CommandStatus, code: int = 200) -> Response:
    response: Response = request.app.state.templates.TemplateResponse(
        request, "_command_status.html", {"status": status}, status_code=code
    )
    return response


def _enqueue(request: Request, kind: str, args: dict[str, object] | None = None) -> Response:
    state = request.app.state
    if kind in command_view.DEDUPE_KINDS and (row := active_command(state.ro_engine, [kind])):
        return _command_status(request, command_view.already_active(command_view.status_for(row)))
    limit = state.settings.command_rate_limit_per_hour
    if count_recent_commands(state.ro_engine) >= limit:
        resets = rate_limit_resets_at(state.ro_engine)
        return _command_status(request, command_view.rate_limited(limit, resets), 429)
    command_id = enqueue_command(state.command_engine, kind, args or {}, _client_ip(request))
    row = get_command(state.ro_engine, command_id)
    assert row is not None
    return _command_status(request, command_view.status_for(row), 202)


def _invalid(request: Request, exc: ValidationError | ValueError) -> Response:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        where = ".".join(str(x) for x in first.get("loc", ()) if x != "positions")
        msg = f"Invalid input{f' ({where})' if where else ''}: {first.get('msg', 'invalid')}"
    else:
        msg = f"Invalid input: {exc}"
    return _command_status(request, command_view.invalid(msg), 400)


@router.get("/commands/{command_id}/status")
def command_status(request: Request, command_id: int, shown: str = "") -> Response:
    """Polled by the inline status: 204 while the state is unchanged (htmx keeps polling and
    doesn't swap), otherwise the new status, which stops polling once it is final."""
    row = get_command(request.app.state.ro_engine, command_id)
    if row is None:
        raise HTTPException(404)
    status = command_view.status_for(row)
    if status.state == shown:
        return Response(status_code=204)
    return _command_status(request, status)


def _blank(v: object) -> str | None:
    text = str(v).strip().replace(",", "") if v is not None else ""
    return text or None


@router.post("/commands/ping")
def command_ping(request: Request) -> Response:
    return _enqueue(request, "ping")


@router.post("/commands/refresh-prices")
def command_refresh_prices(request: Request) -> Response:
    return _enqueue(request, "refresh_prices")


@router.post("/commands/refresh-edgar")
def command_refresh_edgar(request: Request) -> Response:
    return _enqueue(request, "refresh_edgar")


@router.post("/commands/synthesize")
async def command_synthesize(request: Request) -> Response:
    form = await request.form()
    raw = str(form.get("symbol") or "").strip()
    if not raw:
        return _enqueue(request, "synthesize", {})
    try:
        symbol = _known_symbol(request, raw)
    except HTTPException:
        return _invalid(request, ValueError("unknown symbol"))
    return _enqueue(request, "synthesize", {"symbol": symbol})


@router.post("/commands/research-sweep")
def command_research_sweep(request: Request) -> Response:
    return _enqueue(request, "research_sweep")


@router.post("/commands/universe-review")
def command_universe_review(request: Request) -> Response:
    return _enqueue(request, "universe_review")


@router.post("/commands/test-alert")
def command_test_alert(request: Request) -> Response:
    return _enqueue(request, "test_alert")


@router.post("/commands/recompute-strategies")
def command_recompute_strategies(request: Request) -> Response:
    return _enqueue(request, "recompute_strategies")


@router.post("/commands/update-holdings")
async def command_update_holdings(request: Request) -> Response:
    """Holdings form → validated `update_holdings` command. The worker applies it (the
    dashboard never writes holdings). In tiger mode only the cash field is sent."""
    engine = request.app.state.ro_engine
    form = await request.form()
    tiger_mode = load_settings(engine).holdings_source == "tiger"
    try:
        positions = []
        if not tiger_mode:
            for sym in universe_symbols(engine):
                shares = _blank(form.get(f"shares_{sym}"))
                cost = _blank(form.get(f"cost_{sym}"))
                if shares is None or float(shares) == 0:
                    continue
                positions.append(
                    PositionIn.model_validate({"symbol": sym, "shares": shares, "cost_basis": cost})
                )
        update = HoldingsUpdate.model_validate(
            {"positions": positions, "cash": _blank(form.get("cash")) or "0"}
        )
    except (ValidationError, ValueError) as exc:
        return _invalid(request, exc)
    return _enqueue(request, "update_holdings", update.to_args())


@router.post("/commands/portfolio-settings")
async def command_portfolio_settings(request: Request) -> Response:
    form = await request.form()
    values: dict[str, object] = {}
    for key in ("selected_profile", "holdings_source"):
        if (v := _blank(form.get(key))) is not None:
            values[key] = v
    for key in ("whole_shares", "new_cash_only"):
        if (v := _blank(form.get(key))) is not None:
            values[key] = v in ("1", "true", "on")
    try:
        update = SettingsUpdate.model_validate(values)
    except ValidationError as exc:
        return _invalid(request, exc)
    return _enqueue(request, "update_portfolio_settings", update.to_args())


@router.post("/commands/sync-holdings")
def command_sync_holdings(request: Request) -> Response:
    return _enqueue(request, "sync_holdings")


@router.post("/commands/publish-targets")
async def command_publish_targets(request: Request) -> Response:
    """Publish targets now (off-cycle): the owner's decision, optionally citing an event."""
    form = await request.form()
    raw = _blank(form.get("trigger_event_id"))
    args: dict[str, object] = {}
    if raw is not None:
        if not raw.isdigit() or len(raw) > 12:
            return _invalid(request, ValueError("trigger_event_id"))
        args["trigger_event_id"] = int(raw)
    return _enqueue(request, "publish_targets", args)


@router.post("/commands/mark-catalyst")
async def command_mark_catalyst(request: Request) -> Response:
    """Owner marks a catalyst hit / slipped / cancelled, or reopens it; the worker applies it."""
    form = await request.form()
    try:
        mark = CatalystMark.model_validate(
            {
                "catalyst_id": _blank(form.get("catalyst_id")),
                "status": _blank(form.get("status")),
                "event_id": _blank(form.get("event_id")),
                "note": str(form.get("note") or "").strip() or None,
            }
        )
    except ValidationError as exc:
        return _invalid(request, exc)
    return _enqueue(request, "mark_catalyst", mark.model_dump())
