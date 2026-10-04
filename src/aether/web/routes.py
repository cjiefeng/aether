from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from aether import market, sec_view
from aether.alerts import view as alerts_view
from aether.config import load_rubric
from aether.db import health
from aether.db.commands import count_recent_commands, enqueue_command
from aether.providers.prices import US_EASTERN
from aether.risk.flags import open_flags
from aether.security import csrf

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
        request, template, {**context, "csrf_token": token, "disclaimer": DISCLAIMER}
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


# --------------------------------------------------------------------------- commands


def _enqueue(request: Request, kind: str) -> Response:
    state = request.app.state
    limit = state.settings.command_rate_limit_per_hour
    if count_recent_commands(state.ro_engine) >= limit:
        return HTMLResponse(f"Rate limited: max {limit} commands/hour.", status_code=429)
    client_ip = request.client.host if request.client else "unknown"
    command_id = enqueue_command(state.command_engine, kind, {}, requested_by=client_ip)
    return HTMLResponse(f"Queued command #{command_id}.", status_code=202)


@router.post("/commands/ping")
def command_ping(request: Request) -> Response:
    return _enqueue(request, "ping")


@router.post("/commands/refresh-prices")
def command_refresh_prices(request: Request) -> Response:
    return _enqueue(request, "refresh_prices")


@router.post("/commands/refresh-edgar")
def command_refresh_edgar(request: Request) -> Response:
    return _enqueue(request, "refresh_edgar")


@router.post("/commands/test-alert")
def command_test_alert(request: Request) -> Response:
    return _enqueue(request, "test_alert")
