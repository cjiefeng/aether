"""FastAPI app factory. The dashboard reads SQLite via a `mode=ro` engine, and its only write
is `enqueue_command` on the authorizer-restricted command engine. Page paths never call an LLM.

Middleware order, outermost first: security headers → CSRF → routes.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from aether.config import Settings, get_settings
from aether.db.engine import make_command_engine, make_ro_engine
from aether.security.csrf import CSRFMiddleware, CSRFSigner
from aether.security.headers import SecurityHeadersMiddleware
from aether.security.sanitize import register_filters
from aether.web.routes import router

WEB_DIR = Path(__file__).parent


def make_templates() -> Jinja2Templates:
    env = Environment(loader=FileSystemLoader(WEB_DIR / "templates"), autoescape=True)
    register_filters(env)
    return Jinja2Templates(env=env)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
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
    app.state.templates = make_templates()

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    # add_middleware wraps: the last one added is the outermost.
    app.add_middleware(CSRFMiddleware, signer=app.state.csrf)
    app.add_middleware(SecurityHeadersMiddleware)
    return app
