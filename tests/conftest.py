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
from aether.web.app import create_app

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "config"
LAN_IP = "192.168.1.10"  # synthetic client address


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
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def settings(migrated_db: Path) -> Settings:
    return make_settings(migrated_db)


def make_client(settings: Settings, ip: str = LAN_IP) -> TestClient:
    return TestClient(create_app(settings), client=(ip, 50000))


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with make_client(settings) as c:
        yield c
