"""M0 acceptance: CSRF (403), rate limit, security headers, health page."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from aether.config import Settings
from aether.db import migrate
from aether.security.csrf import COOKIE_NAME
from aether.security.headers import CSP
from tests.conftest import make_client, make_settings


def csrf_token(client: TestClient) -> str:
    r = client.get("/health")
    assert r.status_code == 200
    token = client.cookies.get(COOKIE_NAME)
    assert token
    return token


# --- health ----------------------------------------------------------------------------------


def test_health_page_reports_sqlite_version_and_wal(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    import sqlite3

    assert sqlite3.sqlite_version in r.text
    assert ">wal<" in r.text
    assert migrate.head_revision() in r.text
    assert "not financial advice" in r.text


def test_healthz_json(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["journal_mode"] == "wal"
    assert body["sqlite_version"]
    assert body["file_mode"] == "0o600"


def test_healthz_503_without_db(tmp_path: Path) -> None:
    with make_client(make_settings(tmp_path / "nope.db")) as c:
        assert c.get("/healthz").status_code == 503


# --- S2 CSRF + rate limit --------------------------------------------------------------------


def test_command_without_csrf_token_is_403(client: TestClient) -> None:
    csrf_token(client)  # cookie present, header missing
    assert client.post("/commands/ping").status_code == 403


def test_command_without_cookie_is_403(settings: Settings) -> None:
    with make_client(settings) as c:
        token = csrf_token(c)
        c.cookies.clear()
        assert c.post("/commands/ping", headers={"X-CSRF-Token": token}).status_code == 403


def test_command_with_forged_token_is_403(client: TestClient) -> None:
    csrf_token(client)
    forged = "abc.def"
    client.cookies.set(COOKIE_NAME, forged)
    assert client.post("/commands/ping", headers={"X-CSRF-Token": forged}).status_code == 403


def test_command_cross_site_is_403(client: TestClient) -> None:
    token = csrf_token(client)
    for extra in (
        {"Origin": "http://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "null"},
    ):
        r = client.post("/commands/ping", headers={"X-CSRF-Token": token, **extra})
        assert r.status_code == 403, extra


def test_command_with_valid_token_is_queued(client: TestClient) -> None:
    token = csrf_token(client)
    r = client.post(
        "/commands/ping",
        headers={
            "X-CSRF-Token": token,
            "Origin": "http://testserver",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert r.status_code == 202
    assert "Queued command #1" in r.text


def test_command_rate_limit(client: TestClient) -> None:
    token = csrf_token(client)
    codes = [
        client.post("/commands/ping", headers={"X-CSRF-Token": token}).status_code
        for _ in range(11)
    ]
    assert codes == [202] * 10 + [429]


def test_get_never_writes(client: TestClient, rw_engine: object) -> None:
    from sqlalchemy import func, select

    from aether.db.models import commands

    for _ in range(3):
        client.get("/health")
    with rw_engine.connect() as conn:  # type: ignore[attr-defined]
        assert conn.execute(select(func.count()).select_from(commands)).scalar_one() == 0


# --- headers ---------------------------------------------------------------------------------


def test_security_headers(client: TestClient) -> None:
    r = client.get("/health")
    assert r.headers["content-security-policy"] == CSP
    assert "script-src 'self'" in CSP and "unsafe-inline" not in CSP
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "server" not in {k.lower() for k in r.headers}


def test_csrf_cookie_flags(settings: Settings) -> None:
    with make_client(settings) as c:
        r = c.get("/health")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_no_inline_scripts_or_styles(client: TestClient) -> None:
    html = client.get("/health").text
    assert "<script>" not in html and "<script " not in html.replace('<script src="', "")
    assert "style=" not in html and "<style" not in html
