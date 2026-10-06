"""M5 acceptance (S2): every data route needs a session (→ /login), the 6th failed login in
15 minutes gets 429, and the app won't start without a valid password hash and secret."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from aether.config import Settings
from aether.security.auth import (
    SESSION_COOKIE,
    AuthConfig,
    AuthConfigError,
    LoginLimiter,
    PasswordHash,
    hash_password,
    safe_next,
)
from aether.security.csrf import COOKIE_NAME as CSRF_COOKIE
from aether.web.app import create_app
from tests.conftest import (
    TEST_PASSWORD,
    TEST_PASSWORD_HASH,
    login,
    make_client,
    make_settings,
)

DATA_ROUTES = (
    "/",
    "/t/QTUM",
    "/strategies",
    "/holdings",
    "/alerts",
    "/facts",
    "/health",
    "/api/prices/overview",
    "/api/prices/QTUM",
    "/api/strategies/curves?profile=safe",
    "/calibration",
    "/api/reactions/QTUM",
)


@pytest.fixture
def anon(settings: Settings) -> TestClient:
    return make_client(settings, logged_in=False)


def test_unauthenticated_data_routes_redirect_to_login(anon: TestClient) -> None:
    for path in DATA_ROUTES:
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"].startswith("/login?next="), path
    # htmx requests get HX-Redirect instead of a 303 the browser would follow silently.
    r = anon.get("/holdings", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login?next=%2Fholdings"


def test_unauthenticated_commands_are_refused(anon: TestClient) -> None:
    anon.get("/login")
    token = anon.cookies.get(CSRF_COOKIE) or ""
    for path in ("/commands/ping", "/commands/update-holdings", "/commands/sync-holdings"):
        r = anon.post(path, headers={"X-CSRF-Token": token}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_public_paths(anon: TestClient) -> None:
    assert anon.get("/healthz").status_code == 200
    assert anon.get("/static/app.css").status_code == 200
    r = anon.get("/login?next=/holdings")
    assert r.status_code == 200
    assert 'type="password"' in r.text and 'value="/holdings"' in r.text
    assert "Log out" not in r.text


def test_login_sets_session_cookie_and_redirects(anon: TestClient) -> None:
    anon.get("/login")
    token = anon.cookies.get(CSRF_COOKIE) or ""
    r = anon.post(
        "/login",
        data={"password": TEST_PASSWORD, "next": "/holdings"},
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 204 and r.headers["hx-redirect"] == "/holdings"
    cookie = r.headers["set-cookie"].lower()
    assert SESSION_COOKIE in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert "max-age=2592000" in cookie
    assert anon.get("/holdings", follow_redirects=False).status_code == 200


def test_login_requires_csrf(anon: TestClient) -> None:
    anon.get("/login")
    assert anon.post("/login", data={"password": TEST_PASSWORD}).status_code == 403


def test_sixth_failed_login_is_429_and_password_never_logged(
    anon: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="aether.security.auth")
    wrong = "wrong-password-xyz"
    for _ in range(5):
        assert login(anon, wrong) == 401
    assert login(anon, wrong) == 429
    assert login(anon) == 429  # still blocked, even with the right password
    assert wrong not in caplog.text and TEST_PASSWORD not in caplog.text
    assert "failed login from 192.168.1.10" in caplog.text
    # Another client IP is unaffected.
    other = make_client(
        make_settings(anon.app.state.settings.db_path), ip="192.168.1.11", logged_in=False
    )
    assert login(other) == 204


def test_limiter_window_expires() -> None:
    lim = LoginLimiter(max_failures=5, window=900)
    for i in range(5):
        lim.record_failure("ip", now=1000.0 + i)
    assert lim.blocked("ip", now=1100.0)
    assert not lim.blocked("ip", now=1000.0 + 900 + 5)


def test_logout_clears_session(settings: Settings) -> None:
    c = make_client(settings)
    c.get("/")
    token = c.cookies.get(CSRF_COOKIE) or ""
    r = c.post("/logout", headers={"X-CSRF-Token": token})
    assert r.status_code == 204 and r.headers["hx-redirect"] == "/login"
    assert c.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    ("overrides", "msg"),
    [
        ({"dashboard_password_hash": None}, "AETHER_DASHBOARD_PASSWORD_HASH is not set"),
        ({"session_secret": None}, "AETHER_SESSION_SECRET is not set"),
        ({"session_secret": "too-short"}, "at least 32"),
        ({"dashboard_password_hash": "plaintext-password"}, "scrypt"),
        ({"dashboard_password_hash": "scrypt:16:8:1:AAAA:BBBB"}, "too weak"),
        ({"dashboard_password_hash": "scrypt:16384:8:1:!!!:???"}, "malformed"),
    ],
)
def test_missing_or_malformed_secrets_app_wont_start(
    migrated_db: Path, overrides: dict, msg: str
) -> None:
    with pytest.raises(AuthConfigError, match=msg):
        create_app(make_settings(migrated_db, **overrides))


def test_web_main_exits_without_password(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from aether.web import __main__ as web_main

    monkeypatch.setenv("AETHER_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.delenv("AETHER_DASHBOARD_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("AETHER_SESSION_SECRET", "s" * 40)
    called = []
    monkeypatch.setattr(web_main.uvicorn, "run", lambda *a, **k: called.append(1))
    with pytest.raises(SystemExit) as exc:
        web_main.main()
    assert exc.value.code == 1 and called == []


def test_hash_roundtrip_and_session_validation(settings: Settings) -> None:
    ph = PasswordHash.parse(TEST_PASSWORD_HASH)
    assert ph.verify(TEST_PASSWORD) and not ph.verify(TEST_PASSWORD + "x")
    cfg = AuthConfig.from_settings(settings)
    tok = cfg.new_session(now=1_000_000)
    assert cfg.session_valid(tok, now=1_000_001)
    assert not cfg.session_valid(tok, now=1_000_000 + 31 * 24 * 3600)  # expired
    body, sig = tok.split(".")
    assert not cfg.session_valid(body + "." + "0" * len(sig), now=1_000_001)  # forged
    assert not cfg.session_valid(None) and not cfg.session_valid("x")
    # Changing the password invalidates every existing session.
    rotated = AuthConfig.from_settings(
        make_settings(
            settings.db_path, dashboard_password_hash=hash_password("another one!", n=2**14)
        )
    )
    assert not rotated.session_valid(cfg.new_session(now=time.time()))


def test_safe_next() -> None:
    assert safe_next("/holdings?x=1") == "/holdings?x=1"
    for bad in (
        "//evil.example",
        "https://evil.example",
        "/\\evil",
        "",
        None,
        "/login",
        "holdings",
    ):
        assert safe_next(bad) == "/"
