"""Tests run against a temp-file SQLite DB with the real pragmas and migrations (never :memory:).
Network is blocked by pytest-socket (see pyproject addopts)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from starlette.testclient import TestClient

from aether.config import Settings
from aether.db import migrate
from aether.db.engine import ensure_db_file, make_ro_engine, make_rw_engine
from aether.security.auth import MIN_SCRYPT_N, hash_password
from aether.security.csrf import COOKIE_NAME as CSRF_COOKIE
from aether.web.app import create_app

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "config"
LAN_IP = "192.168.1.10"  # synthetic client address
# Synthetic test password; hashed once per session at the minimum accepted scrypt cost.
TEST_PASSWORD = "correct horse test password"
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD, n=MIN_SCRYPT_N)
TEST_SESSION_SECRET = "s" * 48  # synthetic, low-entropy test value


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "aether.db"


@pytest.fixture
def migrated_db(db_path: Path) -> Path:
    ensure_db_file(db_path)
    migrate.upgrade(db_path)
    return db_path


@pytest.fixture
def rw_engine(migrated_db: Path) -> Iterator[Engine]:
    engine = make_rw_engine(migrated_db)
    yield engine
    engine.dispose()


@pytest.fixture
def ro_engine(migrated_db: Path) -> Iterator[Engine]:
    engine = make_ro_engine(migrated_db)
    yield engine
    engine.dispose()


def make_settings(db_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "db_path": db_path,
        "config_dir": CONFIG_DIR,
        "csrf_secret": "x" * 32,  # synthetic, low-entropy test value
        "command_rate_limit_per_hour": 10,
        "dashboard_password_hash": TEST_PASSWORD_HASH,
        "session_secret": TEST_SESSION_SECRET,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def settings(migrated_db: Path) -> Settings:
    return make_settings(migrated_db)


def login(c: TestClient, password: str = TEST_PASSWORD) -> int:
    """Log in through the real form flow (CSRF cookie + header). Returns the status code."""
    c.get("/login")
    token = c.cookies.get(CSRF_COOKIE) or ""
    r = c.post("/login", data={"password": password}, headers={"X-CSRF-Token": token})
    return r.status_code


def make_client(settings: Settings, ip: str = LAN_IP, logged_in: bool = True) -> TestClient:
    c = TestClient(create_app(settings), client=(ip, 50000))
    if logged_in:
        assert login(c) == 204
    return c


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with make_client(settings) as c:
        yield c


def seed_tickers(engine: Engine, rows: list[tuple[str, str]]) -> None:
    """Insert (symbol, type) rows. Use synthetic symbols (ACME, EXMP) unless the code under
    test keys on a real benchmark symbol (QTUM/QQQ/SOXX); prices are always synthetic."""
    from aether.db.dialect import upsert
    from aether.db.engine import write_tx
    from aether.db.models import tickers

    with write_tx(engine) as conn:
        upsert(
            conn,
            tickers,
            [{"symbol": s, "type": t, "active": 1} for s, t in rows],
            key_cols=["symbol"],
        )
