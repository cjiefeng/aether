"""SQLite engines with the spec §2.1 rules.

Three engines, each for one role:

- `make_rw_engine`      worker only. WAL, BEGIN IMMEDIATE on every transaction.
- `make_ro_engine`      dashboard reads. `mode=ro` URI plus `query_only`.
- `make_command_engine` dashboard's single write path: an SQLite authorizer denies everything
                        except INSERT INTO commands (see `db/commands.py`).

Network I/O and LLM calls must never happen inside a `write_tx()` block.
"""

from __future__ import annotations

import logging
import os
import random
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.exc import OperationalError

log = logging.getLogger(__name__)

COMMON_PRAGMAS = (
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-20000",
)


def ensure_db_file(path: Path) -> None:
    """Create the DB file (and parent dir) with owner-only permissions: dir 0700, file 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)


def _attach_listeners(engine: Engine, *, pragmas: tuple[str, ...], begin_sql: str) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: sqlite3.Connection, _rec: Any) -> None:
        # Hand transaction control to SQLAlchemy's `begin` event (documented pysqlite recipe).
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        try:
            for p in pragmas:
                cur.execute(p)
        finally:
            cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        conn.exec_driver_sql(begin_sql)


def make_rw_engine(path: Path) -> Engine:
    """Writer engine (worker process only)."""
    ensure_db_file(path)
    engine = create_engine(f"sqlite+pysqlite:///{path}", connect_args={"timeout": 5})
    _attach_listeners(
        engine, pragmas=("PRAGMA journal_mode=WAL", *COMMON_PRAGMAS), begin_sql="BEGIN IMMEDIATE"
    )
    return engine


def make_ro_engine(path: Path) -> Engine:
    """Read-only engine. Readers never block the writer under WAL."""
    engine = create_engine(
        f"sqlite+pysqlite:///file:{path}?mode=ro&uri=true", connect_args={"timeout": 5}
    )
    _attach_listeners(engine, pragmas=(*COMMON_PRAGMAS, "PRAGMA query_only=ON"), begin_sql="BEGIN")
    return engine


# sqlite3 authorizer action codes allowed on the command engine.
_CMD_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_FUNCTION,
}


def _command_authorizer(
    action: int, arg1: str | None, _arg2: str | None, _db: str | None, _trigger: str | None
) -> int:
    if action in _CMD_ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_INSERT and arg1 == "commands":
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def make_command_engine(path: Path) -> Engine:
    """The dashboard's only write path: INSERT INTO commands, nothing else (SQLite authorizer).

    `mode=rw` so the dashboard can never create the DB file; the worker owns its lifecycle.
    """
    engine = create_engine(
        f"sqlite+pysqlite:///file:{path}?mode=rw&uri=true", connect_args={"timeout": 5}
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: sqlite3.Connection, _rec: Any) -> None:
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        try:
            for p in COMMON_PRAGMAS:
                cur.execute(p)
        finally:
            cur.close()
        # Installed after the pragmas so they aren't subject to it.
        dbapi_conn.set_authorizer(_command_authorizer)

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def is_busy_error(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", exc)
    if isinstance(orig, sqlite3.OperationalError):
        code = getattr(orig, "sqlite_errorcode", None)
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
        msg = str(orig).lower()
        return "database is locked" in msg or "database is busy" in msg
    return False


@contextmanager
def write_tx(engine: Engine, *, attempts: int = 5) -> Iterator[Connection]:
    """Short write transaction (BEGIN IMMEDIATE). Lock acquisition is retried on SQLITE_BUSY.

    Keep the body small and batched; never do network I/O inside it.
    """
    for attempt in range(1, attempts + 1):
        conn = engine.connect()
        try:
            trans = conn.begin()
        except OperationalError as exc:
            conn.close()
            if not is_busy_error(exc) or attempt == attempts:
                raise
            delay = min(2.0, 0.05 * 2**attempt) * (0.5 + random.random())  # noqa: S311
            log.warning("sqlite busy on BEGIN IMMEDIATE (attempt %d), retrying", attempt)
            time.sleep(delay)
            continue
        try:
            yield conn
            trans.commit()
        except BaseException:
            trans.rollback()
            raise
        finally:
            conn.close()
        return
