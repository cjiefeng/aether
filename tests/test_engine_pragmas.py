from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, text
from sqlalchemy.exc import DatabaseError, OperationalError

from aether.db.engine import make_command_engine, make_rw_engine, write_tx
from aether.db.models import commands, tickers
from aether.db.types import utcnow_iso


def pragma(engine: Engine, name: str) -> object:
    with engine.connect() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar_one()


def test_rw_pragmas(rw_engine: Engine) -> None:
    assert pragma(rw_engine, "journal_mode") == "wal"
    assert pragma(rw_engine, "busy_timeout") == 5000
    assert pragma(rw_engine, "foreign_keys") == 1
    assert pragma(rw_engine, "synchronous") == 1  # NORMAL
    assert pragma(rw_engine, "temp_store") == 2  # MEMORY
    assert pragma(rw_engine, "cache_size") == -20000


def test_ro_pragmas_and_wal_visible(ro_engine: Engine) -> None:
    assert pragma(ro_engine, "journal_mode") == "wal"
    assert pragma(ro_engine, "busy_timeout") == 5000
    assert pragma(ro_engine, "foreign_keys") == 1
    assert pragma(ro_engine, "query_only") == 1


def test_rw_transactions_are_immediate(rw_engine: Engine) -> None:
    # BEGIN IMMEDIATE takes the write lock up front: a second writer can't begin.
    other = make_rw_engine(Path(str(rw_engine.url.database)))
    raw = other.raw_connection()
    try:
        raw.driver_connection.execute("PRAGMA busy_timeout=50")  # type: ignore[union-attr]
        with write_tx(rw_engine) as conn:
            conn.execute(insert(tickers).values(symbol="ACME", type="context"))
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                raw.driver_connection.execute("BEGIN IMMEDIATE")  # type: ignore[union-attr]
    finally:
        raw.close()
        other.dispose()


def test_ro_engine_rejects_writes(ro_engine: Engine) -> None:
    with ro_engine.connect() as conn, pytest.raises(OperationalError):
        conn.execute(insert(tickers).values(symbol="ACME", type="context"))


def test_command_engine_only_inserts_commands(migrated_db: Path) -> None:
    engine = make_command_engine(migrated_db)
    with write_tx(engine) as conn:
        conn.execute(
            insert(commands).values(
                kind="ping", args="{}", requested_at=utcnow_iso(), requested_by="test"
            )
        )
    denied = [
        "UPDATE commands SET status='done'",
        "DELETE FROM commands",
        "INSERT INTO tickers(symbol, type) VALUES ('ACME', 'context')",
        "CREATE TABLE evil(x INTEGER)",
        "DROP TABLE commands",
        "PRAGMA journal_mode=DELETE",
        "ATTACH DATABASE '/tmp/x.db' AS x",
    ]
    for sql in denied:
        with pytest.raises(DatabaseError, match="not authorized"), write_tx(engine) as conn:
            conn.exec_driver_sql(sql)
    engine.dispose()


def test_command_engine_cannot_create_db(tmp_path: Path) -> None:
    engine = make_command_engine(tmp_path / "missing.db")
    with pytest.raises(OperationalError):
        engine.connect()
    assert not (tmp_path / "missing.db").exists()


def test_db_file_is_owner_only(migrated_db: Path) -> None:
    assert stat.S_IMODE(os.stat(migrated_db).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(migrated_db.parent).st_mode) == 0o700


def test_web_never_uses_writer_engine() -> None:
    web = Path(__file__).resolve().parent.parent / "src" / "aether" / "web"
    for py in web.rglob("*.py"):
        src = py.read_text()
        assert "make_rw_engine" not in src and "write_tx" not in src, py
