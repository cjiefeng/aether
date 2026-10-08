"""M11 acceptance: a restore from backup reproduces the dashboard (drill + restore)."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from aether.ops.backup import backup
from aether.ops.restore import DrillFailed, drill, main, newest_backup, restore
from aether.worker import startup
from tests.conftest import make_settings


@pytest.fixture
def live(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = make_settings(tmp_path / "data" / "aether.db")
    engine = startup(s)  # migrated + watchlist/facts synced, like a real worker
    yield s
    engine.dispose()


def test_drill_restores_newest_backup_and_renders_the_dashboard(live) -> None:  # type: ignore[no-untyped-def]
    path = backup(live.db_path, live.resolved_backup_dir)
    assert newest_backup(live.resolved_backup_dir) == path
    res = drill(live)
    assert res.backup == path.name and not res.migrated
    assert set(res.pages.values()) == {200}
    assert "/ops" in res.pages and any(p.startswith("/t/") for p in res.pages)
    assert res.counts["tickers"][0] == res.counts["tickers"][1] > 0
    # The live DB was only read: still there, still 0600.
    assert stat.S_IMODE(os.stat(live.db_path).st_mode) == 0o600


def test_drill_fails_on_a_corrupt_backup(live) -> None:  # type: ignore[no-untyped-def]
    path = backup(live.db_path, live.resolved_backup_dir)
    data = bytearray(path.read_bytes())
    data[0:16] = b"not a sqlite db!"
    path.write_bytes(bytes(data))
    with pytest.raises(DrillFailed, match="integrity"):
        drill(live)


def test_drill_fails_without_a_backup(live) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DrillFailed, match="no backup found"):
        drill(live)


def test_restore_needs_the_stack_stopped_and_keeps_the_previous_db(live) -> None:  # type: ignore[no-untyped-def]
    path = backup(live.db_path, live.resolved_backup_dir)
    # Live DB changes after the backup; the restore must bring back the backup's content.
    con = sqlite3.connect(live.db_path)
    con.execute("UPDATE tickers SET active = 0")
    con.commit()
    con.close()

    with pytest.raises(SystemExit, match="stop it first"):
        restore(live, path, stack_stopped=False)
    kept = restore(live, path, stack_stopped=True)
    assert kept is not None and kept.name.startswith("aether.db.pre-restore-")
    assert stat.S_IMODE(os.stat(live.db_path).st_mode) == 0o600
    con = sqlite3.connect(live.db_path)
    try:
        assert con.execute("SELECT min(active) FROM tickers").fetchone()[0] == 1
    finally:
        con.close()
    con = sqlite3.connect(kept)
    try:
        assert con.execute("SELECT max(active) FROM tickers").fetchone()[0] == 0
    finally:
        con.close()


def test_cli_drill_exit_codes(live, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AETHER_DB_PATH", str(live.db_path))
    monkeypatch.setenv("AETHER_CONFIG_DIR", str(live.config_dir))
    # Keep pytest's log capture: the CLI would bind the root handler to this test's stderr.
    monkeypatch.setattr("aether.logs.setup_logging", lambda _s: None)
    assert main(["drill"]) == 1
    assert "no backup found" in capsys.readouterr().err
    backup(live.db_path, live.resolved_backup_dir)
    assert main(["drill"]) == 0
    assert "restore drill passed" in capsys.readouterr().out
