from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from aether.db import health
from aether.db.commands import count_recent_commands, enqueue_command
from aether.security import csrf

router = APIRouter()

DISCLAIMER = "Personal research tool, not financial advice."


def _render(request: Request, template: str, context: dict[str, object]) -> HTMLResponse:
    token = csrf.token_for(request, request.app.state.csrf)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, template, {**context, "csrf_token": token, "disclaimer": DISCLAIMER}
    )
    csrf.set_cookie(response, token)
    return response


@router.get("/", response_class=HTMLResponse)
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


@router.post("/commands/ping")
def command_ping(request: Request) -> Response:
    state = request.app.state
    limit = state.settings.command_rate_limit_per_hour
    if count_recent_commands(state.ro_engine) >= limit:
        return HTMLResponse(f"Rate limited: max {limit} commands/hour.", status_code=429)
    client_ip = request.client.host if request.client else "unknown"
    command_id = enqueue_command(state.command_engine, "ping", {}, requested_by=client_ip)
    return HTMLResponse(f"Queued command #{command_id}.", status_code=202)
