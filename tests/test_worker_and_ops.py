from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine, insert, select

from aether.db import migrate
from aether.db.commands import enqueue_command
from aether.db.engine import make_command_engine
from aether.db.models import commands, job_runs, tickers
from aether.jobs import JobResult, process_commands, run_job
from aether.ops.backup import backup
from aether.worker import startup
from tests.cassettes import mount_cassette
from tests.conftest import make_settings


def test_worker_startup_migrates_and_syncs(tmp_path: Path) -> None:
    s = make_settings(tmp_path / "w" / "aether.db")
    engine = startup(s)
    startup(s).dispose()  # idempotent second start
    with engine.connect() as conn:
        symbols = conn.execute(select(tickers.c.symbol)).scalars().all()
    assert len(symbols) == 16 and "QTUM" in symbols
    engine.dispose()


def test_run_job_records_ok_and_failed(rw_engine: Engine) -> None:
    run_job(rw_engine, "good", lambda: JobResult(rows_written=3, provider="synthetic"))

    def boom() -> JobResult:
        raise RuntimeError("synthetic failure")

    run_job(rw_engine, "bad", boom)
    with rw_engine.connect() as conn:
        rows = {r.job: r for r in conn.execute(select(job_runs)).all()}
    assert rows["good"].status == "ok" and rows["good"].rows_written == 3
    assert rows["bad"].status == "failed" and "synthetic failure" in rows["bad"].error


def test_commands_roundtrip(rw_engine: Engine, migrated_db: Path) -> None:
    cmd_engine = make_command_engine(migrated_db)
    cid = enqueue_command(cmd_engine, "ping", {}, requested_by="192.168.1.10")
    with rw_engine.begin() as conn:  # simulate a stale/unknown kind written by an older app
        conn.execute(
            insert(commands).values(
                kind="mystery", args="{}", requested_at="2026-01-01T00:00:00Z", requested_by="x"
            )
        )
    assert process_commands(rw_engine).rows_written == 2
    with rw_engine.connect() as conn:
        rows = {r.id: r for r in conn.execute(select(commands)).all()}
    assert rows[cid].status == "done" and "pong" in json.loads(rows[cid].result)
    assert rows[cid + 1].status == "rejected"
    cmd_engine.dispose()


def test_backup_is_consistent_and_private(
    rw_engine: Engine, migrated_db: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "backups"
    old = dest / "aether-20200101.db"
    dest.mkdir()
    old.write_bytes(b"")
    out = backup(migrated_db, dest, keep_days=14, today=date(2026, 10, 4))
    assert out.name == "aether-20261004.db"
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert not old.exists()
    con = sqlite3.connect(out)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (
            migrate.head_revision(),
        )
    finally:
        con.close()


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_network_is_blocked() -> None:
    with pytest.raises(Exception, match=r"(?i)socket"):
        socket.create_connection(("example.com", 80), timeout=1)


def test_cassette_replay() -> None:
    with respx.mock(assert_all_called=True) as router:
        mount_cassette(router, "synthetic_acme_example")
        r = httpx.get("https://example.test/acme/status.json")
    assert r.status_code == 200 and r.json()["ticker"] == "ACME"
