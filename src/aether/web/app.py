"""FastAPI app factory. The dashboard reads SQLite via a `mode=ro` engine, and its only write
is `enqueue_command` on the authorizer-restricted command engine. Page paths never call an LLM.

Middleware order, outermost first: security headers → CSRF → auth (M5 password) → routes.
The app refuses to start without a valid password hash and session secret (S2, fail closed).
"""

from __future__ import annotations

import secrets
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from aether.config import Settings, get_settings
from aether.db.engine import make_command_engine, make_ro_engine
from aether.security.auth import AuthConfig, AuthMiddleware, LoginLimiter
from aether.security.csrf import CSRFMiddleware, CSRFSigner
from aether.security.headers import SecurityHeadersMiddleware
from aether.security.sanitize import register_filters
from aether.web import command_view
from aether.web.routes import router

WEB_DIR = Path(__file__).parent


def pct(value: float | None, signed: bool = True) -> str:
    """Fraction -> percent text (plain str, so it's autoescaped like everything else)."""
    if value is None:
        return "—"
    return f"{value * 100:+.1f}%" if signed else f"{value * 100:.1f}%"


def usd(value: Decimal | None) -> str:
    """Decimal dollars -> compact text ($1.25B, $350.0M, $12.50)."""
    if value is None:
        return "—"
    a = abs(value)
    for scale, suffix in ((Decimal(10) ** 9, "B"), (Decimal(10) ** 6, "M")):
        if a >= scale:
            return f"${value / scale:,.2f}{suffix}"
    return f"${value:,.2f}"


def intc(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def make_templates() -> Jinja2Templates:
    env = Environment(loader=FileSystemLoader(WEB_DIR / "templates"), autoescape=True)
    register_filters(env)
    env.filters["pct"] = pct
    env.filters["usd"] = usd
    env.filters["intc"] = intc
    return Jinja2Templates(env=env)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    auth = AuthConfig.from_settings(settings)  # raises: no password, no app
    secret = (
        settings.csrf_secret.get_secret_value().encode()
        if settings.csrf_secret
        else secrets.token_bytes(32)
    )

    app = FastAPI(title="Aether", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.ro_engine = make_ro_engine(settings.db_path)
    app.state.command_engine = make_command_engine(settings.db_path)
    app.state.csrf = CSRFSigner(secret)
    app.state.auth = auth
    app.state.login_limiter = LoginLimiter()
    app.state.templates = make_templates()
    # Page loads render any in-progress command next to its button (issue #18).
    app.state.templates.env.globals["active_command_status"] = lambda request, *kinds: (
        command_view.active_status(request.app.state.ro_engine, *kinds)
    )

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    # add_middleware wraps: the last one added is the outermost.
    app.add_middleware(AuthMiddleware, config=auth)
    app.add_middleware(CSRFMiddleware, signer=app.state.csrf)
    app.add_middleware(SecurityHeadersMiddleware)
    return app
