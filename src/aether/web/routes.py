from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from aether import market, news_view, sec_view
from aether.alerts import view as alerts_view
from aether.config import PROFILES, load_rubric
from aether.db import health
from aether.db.commands import count_recent_commands, enqueue_command
from aether.portfolio import holdings_view
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
from aether.security import auth, csrf

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
            "flags": open_flags(engine, rubric.risk_flags, today, pure),
            "risk_events": sec_view.recent_events(engine, days=30),
            "sec_fresh": sec_view.sec_freshness(engine),
            "alerts": alerts_view.recent_alerts(engine, limit=5),
            "drift": holdings_view.drift_card(engine),
        },
    )


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
            "flags": open_flags(engine, rubric.risk_flags, today, [symbol]),
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
    return _render(
        request,
        "ticker.html",
        {
            "s": summary,
            "type_label": TYPE_LABELS[type_],
            "n_bars": len(series),
            "sec": sec,
            "code_labels": sec_view.CODE_LABELS,
            "news": news_view.news_rows(engine, symbol=symbol, limit=10)
            if type_ in ("etf", "pure_play")
            else [],
            "origin_labels": news_view.ORIGIN_LABELS,
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


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request) -> HTMLResponse:
    engine = request.app.state.ro_engine
    return _render(
        request,
        "alerts.html",
        {
            "alerts": alerts_view.recent_alerts(engine, limit=100),
            "delivery": alerts_view.delivery_status(engine),
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
    return _render(
        request,
        "holdings.html",
        {
            "h": holdings_view.holdings_page(engine, _today()),
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


@router.get("/api/strategies/curves")
def api_strategy_curves(request: Request, profile: str = "safe") -> JSONResponse:
    if profile not in PROFILES:
        raise HTTPException(404)
    return JSONResponse(strategies_view.curves_for(request.app.state.ro_engine, profile))


# --------------------------------------------------------------------------- commands


def _enqueue(request: Request, kind: str, args: dict[str, object] | None = None) -> Response:
    state = request.app.state
    limit = state.settings.command_rate_limit_per_hour
    if count_recent_commands(state.ro_engine) >= limit:
        return HTMLResponse(f"Rate limited: max {limit} commands/hour.", status_code=429)
    command_id = enqueue_command(state.command_engine, kind, args or {}, _client_ip(request))
    return HTMLResponse(f"Queued command #{command_id}.", status_code=202)


def _invalid(exc: ValidationError | ValueError) -> HTMLResponse:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        where = ".".join(str(x) for x in first.get("loc", ()) if x != "positions")
        msg = f"Invalid input{f' ({where})' if where else ''}: {first.get('msg', 'invalid')}"
    else:
        msg = f"Invalid input: {exc}"
    return HTMLResponse(msg[:300], status_code=400)


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


@router.post("/commands/research-sweep")
def command_research_sweep(request: Request) -> Response:
    return _enqueue(request, "research_sweep")


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
        return _invalid(exc)
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
        return _invalid(exc)
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
            return HTMLResponse("Invalid input: trigger_event_id", status_code=400)
        args["trigger_event_id"] = int(raw)
    return _enqueue(request, "publish_targets", args)
