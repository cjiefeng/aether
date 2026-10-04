"""S2: CSRF protection for every state-changing request.

There's no login, so the threat is a malicious page in a LAN browser firing commands.
Defence:
1. Signed double-submit token. An HttpOnly, SameSite=Strict cookie holds `nonce.hmac`, and
   the request must echo the same value in the `X-CSRF-Token` header (HTMX sends it via
   `hx-headers`).
2. If the browser sends `Origin` or `Sec-Fetch-Site`, it must be same-origin.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE_NAME = "aether_csrf"
HEADER_NAME = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class CSRFSigner:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("CSRF secret must be at least 16 bytes")
        self._secret = secret

    def _sig(self, nonce: str) -> str:
        return hmac.new(self._secret, nonce.encode(), hashlib.sha256).hexdigest()

    def new_token(self) -> str:
        nonce = secrets.token_urlsafe(32)
        return f"{nonce}.{self._sig(nonce)}"

    def is_valid(self, token: str | None) -> bool:
        if not token or token.count(".") != 1:
            return False
        nonce, sig = token.split(".")
        return bool(nonce) and hmac.compare_digest(sig, self._sig(nonce))


def token_for(request: Request, signer: CSRFSigner) -> str:
    """Current valid cookie token, or a fresh one (the caller sets it via `set_cookie`)."""
    existing = request.cookies.get(COOKIE_NAME)
    if existing is not None and signer.is_valid(existing):
        return existing
    return signer.new_token()


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="strict", path="/", max_age=7 * 24 * 3600
    )


def _same_origin(request: Request) -> bool:
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None and sfs not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin == "null":
        return False
    host = request.headers.get("host")
    return host is not None and urlsplit(origin).netloc == host


class CSRFMiddleware:
    def __init__(self, app: ASGIApp, signer: CSRFSigner) -> None:
        self.app = app
        self.signer = signer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        cookie = request.cookies.get(COOKIE_NAME)
        header = request.headers.get(HEADER_NAME)
        ok = (
            _same_origin(request)
            and self.signer.is_valid(cookie)
            and header is not None
            and cookie is not None
            and hmac.compare_digest(header, cookie)
        )
        if not ok:
            await PlainTextResponse("CSRF check failed", status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)
