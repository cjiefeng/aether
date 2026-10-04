from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from aether import market
from aether.db import health
from aether.db.commands import count_recent_commands, enqueue_command
from aether.providers.prices import US_EASTERN
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
    return _render(
        request,
        "ticker.html",
        {"s": summary, "type_label": TYPE_LABELS[type_], "n_bars": len(series)},
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
