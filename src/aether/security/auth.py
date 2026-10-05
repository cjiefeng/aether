"""S2 (M5): one site-wide password, no user accounts.

- The password is stored only as an scrypt hash (`AETHER_DASHBOARD_PASSWORD_HASH`), format
  `scrypt:<n>:<r>:<p>:<salt b64>:<hash b64>` (no `$`, so docker compose never
  interpolates it). `python -m aether.security.auth hash` (via
  `make hash-password`) prompts for it and prints the hash and a fresh session secret.
- A successful login sets an HMAC-signed session cookie (`AETHER_SESSION_SECRET`): HttpOnly,
  SameSite=Strict, 30 days. The cookie carries a fingerprint of the password hash, so changing
  the password logs every browser out.
- Failed logins are rate-limited per client IP (in memory) and logged without the password.
- Fail closed: `AuthConfig.from_settings` raises if either env var is missing or malformed, and
  the app refuses to start.

Every route needs a valid session except `/login`, static assets and `/healthz`.
"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import hmac
import json
import logging
import re
import secrets
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from aether.config import Settings

log = logging.getLogger(__name__)

SESSION_COOKIE = "aether_session"
SESSION_MAX_AGE = 30 * 24 * 3600
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
MIN_SCRYPT_N = 2**14
SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_SESSION_SECRET_LEN = 32

PUBLIC_PATHS = frozenset({"/login", "/healthz"})
PUBLIC_PREFIXES = ("/static/",)


class AuthConfigError(ValueError):
    pass


# --------------------------------------------------------------------------- password hash


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise binascii.Error("not urlsafe base64")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    if not password:
        raise ValueError("empty password")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM)
    return f"scrypt:{n}:{r}:{p}:{_b64e(salt)}:{_b64e(dk)}"


@dataclass(frozen=True)
class PasswordHash:
    n: int
    r: int
    p: int
    salt: bytes
    dk: bytes

    @classmethod
    def parse(cls, text: str) -> PasswordHash:
        parts = text.strip().split(":")
        if len(parts) != 6 or parts[0] != "scrypt":
            raise AuthConfigError("password hash must look like scrypt:n:r:p:salt:hash")
        try:
            n, r, p = (int(x) for x in parts[1:4])
            salt, dk = _b64d(parts[4]), _b64d(parts[5])
        except (ValueError, binascii.Error) as exc:
            raise AuthConfigError("password hash is malformed") from exc
        if n < MIN_SCRYPT_N or n & (n - 1) or not 1 <= r <= 32 or not 1 <= p <= 16:
            raise AuthConfigError("password hash parameters are too weak or invalid")
        if len(salt) < 16 or len(dk) < 32:
            raise AuthConfigError("password hash salt/key too short")
        return cls(n, r, p, salt, dk)

    def verify(self, password: str) -> bool:
        dk = hashlib.scrypt(
            password.encode(),
            salt=self.salt,
            n=self.n,
            r=self.r,
            p=self.p,
            maxmem=SCRYPT_MAXMEM,
            dklen=len(self.dk),
        )
        return hmac.compare_digest(dk, self.dk)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.salt + self.dk).hexdigest()[:16]


# --------------------------------------------------------------------------- sessions


@dataclass(frozen=True)
class AuthConfig:
    password: PasswordHash
    session_key: bytes

    @classmethod
    def from_settings(cls, settings: Settings) -> AuthConfig:
        if settings.dashboard_password_hash is None:
            raise AuthConfigError("AETHER_DASHBOARD_PASSWORD_HASH is not set")
        if settings.session_secret is None:
            raise AuthConfigError("AETHER_SESSION_SECRET is not set")
        secret = settings.session_secret.get_secret_value()
        if len(secret) < MIN_SESSION_SECRET_LEN:
            raise AuthConfigError(
                f"AETHER_SESSION_SECRET must be at least {MIN_SESSION_SECRET_LEN} characters"
            )
        return cls(
            PasswordHash.parse(settings.dashboard_password_hash.get_secret_value()),
            secret.encode(),
        )

    def _sig(self, body: str) -> str:
        return hmac.new(self.session_key, body.encode(), hashlib.sha256).hexdigest()

    def new_session(self, now: float | None = None) -> str:
        iat = int(now if now is not None else time.time())
        payload = {"iat": iat, "exp": iat + SESSION_MAX_AGE, "pv": self.password.fingerprint}
        body = _b64e(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        return f"{body}.{self._sig(body)}"

    def session_valid(self, token: str | None, now: float | None = None) -> bool:
        if not token or token.count(".") != 1:
            return False
        body, sig = token.split(".")
        if not hmac.compare_digest(sig, self._sig(body)):
            return False
        try:
            payload = json.loads(_b64d(body))
        except (ValueError, binascii.Error):
            return False
        if not isinstance(payload, dict):
            return False
        t = now if now is not None else time.time()
        exp = payload.get("exp")
        return isinstance(exp, int) and t < exp and payload.get("pv") == self.password.fingerprint


def set_session_cookie(response: Response, token: str) -> None:
    # Not `secure`: the dashboard is plain HTTP on the LAN (README).
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", path="/", max_age=SESSION_MAX_AGE
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")


# --------------------------------------------------------------------------- login rate limit


class LoginLimiter:
    """Failed logins per client IP in a sliding window. In memory: the dashboard can't write
    SQLite, so a restart resets the counts."""

    def __init__(
        self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW_SECONDS
    ) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, ip: str, now: float) -> deque[float]:
        q = self._failures.setdefault(ip, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        return q

    def blocked(self, ip: str, now: float | None = None) -> bool:
        with self._lock:
            return len(self._trim(ip, now or time.monotonic())) >= self.max_failures

    def record_failure(self, ip: str, now: float | None = None) -> int:
        with self._lock:
            q = self._trim(ip, now or time.monotonic())
            q.append(now or time.monotonic())
            return len(q)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


# --------------------------------------------------------------------------- middleware


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def safe_next(value: str | None) -> str:
    """Only same-site absolute paths; anything else goes to the Overview."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    if any(c in value for c in "\r\n\\") or value.startswith("/login"):
        return "/"
    return value


class AuthMiddleware:
    def __init__(self, app: ASGIApp, config: AuthConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_public(scope["path"]):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if self.config.session_valid(request.cookies.get(SESSION_COOKIE)):
            await self.app(scope, receive, send)
            return
        target = scope["path"]
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode("latin-1")
        login = "/login?next=" + quote(safe_next(target), safe="")
        response: Response
        if request.headers.get("hx-request"):
            response = Response(status_code=401, headers={"HX-Redirect": login})
        else:
            response = RedirectResponse(login, status_code=303)
        await response(scope, receive, send)


# --------------------------------------------------------------------------- CLI


def _cli(argv: list[str]) -> int:
    if argv[:1] != ["hash"]:
        print("usage: python -m aether.security.auth hash", file=sys.stderr)
        return 2
    pw = getpass.getpass("Dashboard password: ")
    if len(pw) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat: ") != pw:
        print("Passwords don't match.", file=sys.stderr)
        return 1
    print("# Add these to .env (mode 0600):")
    print(f"AETHER_DASHBOARD_PASSWORD_HASH={hash_password(pw)}")
    print(f"AETHER_SESSION_SECRET={secrets.token_urlsafe(48)}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
