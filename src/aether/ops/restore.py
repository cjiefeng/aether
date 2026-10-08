"""Backup restore drill and restore (spec M11: "restore from backup reproduces the dashboard").

    python -m aether.ops.restore drill                 # weekly worker job; `make restore-drill`
    python -m aether.ops.restore restore FILE --stack-stopped   # `make restore BACKUP=FILE`

**Drill.** It never touches the live DB. It copies the newest nightly backup to a temp dir, switches
the copy to WAL (as the worker would), checks `PRAGMA integrity_check`, migrates the copy forward
when it is behind the code (as the worker does at start), and compares row counts with the live
DB. Then it renders the main dashboard pages against the copy in-process, with a throwaway
password and session, and expects HTTP 200 from each one. Any failure raises `DrillFailed`, so the
`restore_drill` job fails and the job-failing alert covers it.

**Restore** replaces the live DB with a backup. Run it only with the stack stopped (the Makefile
stops it and passes `--stack-stopped`). The current DB is kept as
`aether.db.pre-restore-<UTC stamp>`; the `-wal`/`-shm` files are moved aside with it.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from aether.config import Settings, load_watchlist
from aether.db import migrate
from aether.ops.backup import NAME_RE

log = logging.getLogger(__name__)

# Tables that must not come back empty when the live table has rows.
CORE_TABLES = ("tickers", "prices_daily", "job_runs")
PAGES = (
    "/",
    "/feed",
    "/news",
    "/catalysts",
    "/strategies",
    "/holdings",
    "/review",
    "/facts",
    "/ops",
)


class DrillFailed(RuntimeError):
    pass


@dataclass
class DrillResult:
    backup: str
    revision: str | None
    migrated: bool
    pages: dict[str, int] = field(default_factory=dict)
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)  # table → (backup, live)

    def summary(self) -> str:
        rows = sum(b for b, _ in self.counts.values())
        return (
            f"{self.backup}: integrity ok, revision {self.revision}"
            + (" (migrated forward)" if self.migrated else "")
            + f", {rows} rows, {len(self.pages)} pages rendered"
        )


def newest_backup(backup_dir: Path) -> Path | None:
    found = sorted(p for p in backup_dir.glob("aether-*.db") if NAME_RE.match(p.name))
    return found[-1] if found else None


def integrity(path: Path) -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(con.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.DatabaseError as exc:  # e.g. "file is not a database"
        return f"{type(exc).__name__}: {exc}"
    finally:
        con.close()


def _revision(path: Path) -> str | None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()
    return str(row[0]) if row else None


def table_counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        # Names come from sqlite_master of our own file, quoted as identifiers.
        return {
            n: int(con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0])  # noqa: S608
            for n in names
        }
    finally:
        con.close()


def _render_pages(settings: Settings, db_copy: Path) -> dict[str, int]:
    # Imported here: the worker image has the web package, but nothing else in the worker needs it.
    from starlette.testclient import TestClient

    from aether.security.auth import MIN_SCRYPT_N, SESSION_COOKIE, AuthConfig, hash_password
    from aether.web.app import create_app

    drill = settings.model_copy(
        update={
            "db_path": db_copy,
            "dashboard_password_hash": SecretStr(
                hash_password(secrets.token_hex(16), n=MIN_SCRYPT_N)
            ),
            "session_secret": SecretStr(secrets.token_urlsafe(48)),
            "csrf_secret": SecretStr(secrets.token_urlsafe(32)),
        }
    )
    app = create_app(drill)
    pages = list(PAGES)
    watch = load_watchlist(settings.config_dir)
    first = next((t.symbol for t in watch.tickers if t.type == "pure_play" and t.active), None)
    if first:
        pages.insert(1, f"/t/{first}")
    out: dict[str, int] = {}
    try:
        with TestClient(app, base_url="http://drill.localhost") as client:
            client.cookies.set(SESSION_COOKIE, AuthConfig.from_settings(drill).new_session())
            for path in pages:
                out[path] = client.get(path, follow_redirects=False).status_code
    finally:
        app.state.ro_engine.dispose()
        app.state.command_engine.dispose()
    return out


def drill(settings: Settings, backup: Path | None = None) -> DrillResult:
    backup = backup or newest_backup(settings.resolved_backup_dir)
    if backup is None:
        raise DrillFailed(f"no backup found in {settings.resolved_backup_dir}")
    with tempfile.TemporaryDirectory(prefix="aether-drill-") as tmp:
        copy = Path(tmp) / "aether.db"
        shutil.copyfile(backup, copy)
        os.chmod(copy, 0o600)
        check = integrity(copy)
        if check != "ok":
            raise DrillFailed(f"{backup.name}: integrity_check: {check[:200]}")
        con = sqlite3.connect(copy)
        try:
            con.execute("PRAGMA journal_mode=WAL")  # as the worker runs it
        finally:
            con.close()
        rev = _revision(copy)
        if rev is None:
            raise DrillFailed(f"{backup.name}: no alembic_version (not an Aether database?)")
        migrated = rev != migrate.head_revision()
        if migrated:
            migrate.upgrade(copy)
        result = DrillResult(backup=backup.name, revision=rev, migrated=migrated)

        restored = table_counts(copy)
        live = table_counts(settings.db_path) if settings.db_path.exists() else {}
        result.counts = {t: (restored.get(t, 0), live.get(t, 0)) for t in sorted(restored)}
        empty = [t for t in CORE_TABLES if live.get(t, 0) and not restored.get(t, 0)]
        if empty:
            raise DrillFailed(f"{backup.name}: empty core tables: {', '.join(empty)}")

        result.pages = _render_pages(settings, copy)
        bad = {p: s for p, s in result.pages.items() if s != 200}
        if bad:
            raise DrillFailed(f"{backup.name}: pages failed to render: {bad}")
    log.info("restore drill passed: %s", result.summary())
    return result


def restore(settings: Settings, backup: Path, *, stack_stopped: bool) -> Path | None:
    """Replace the live DB with `backup`. Returns where the previous DB was kept (if any)."""
    if not stack_stopped:
        raise SystemExit(
            "refusing to restore while the stack may be running: stop it first "
            "(`make restore BACKUP=...` does this) and pass --stack-stopped"
        )
    if not backup.is_file():
        raise SystemExit(f"backup not found: {backup}")
    check = integrity(backup)
    if check != "ok":
        raise SystemExit(f"{backup.name} failed integrity_check: {check[:200]}")
    live = settings.db_path
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    kept: Path | None = None
    if live.exists():
        kept = live.with_name(f"{live.name}.pre-restore-{stamp}")
        live.replace(kept)
    for suffix in ("-wal", "-shm"):
        side = live.with_name(live.name + suffix)
        if side.exists():
            side.replace(live.with_name(f"{live.name}.pre-restore-{stamp}{suffix}"))
    tmp = live.with_name(live.name + ".restoring")
    shutil.copyfile(backup, tmp)
    os.chmod(tmp, 0o600)
    tmp.replace(live)
    log.info("restored %s from %s (previous DB kept as %s)", live, backup, kept)
    return kept


def main(argv: list[str] | None = None) -> int:
    from aether.config import get_settings
    from aether.logs import setup_logging

    parser = argparse.ArgumentParser(prog="python -m aether.ops.restore")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drill", help="restore the newest backup into a temp dir and check it")
    d.add_argument("backup", nargs="?", help="a backup file (default: the newest)")
    r = sub.add_parser("restore", help="replace the live DB with a backup (stack stopped)")
    r.add_argument("backup", help="backup file name in the backup dir, or a path")
    r.add_argument("--stack-stopped", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings)

    def resolve(name: str) -> Path:
        p = Path(name)
        return p if p.is_absolute() or p.exists() else settings.resolved_backup_dir / name

    if args.cmd == "drill":
        try:
            res = drill(settings, resolve(args.backup) if args.backup else None)
        except DrillFailed as exc:
            print(f"restore drill FAILED: {exc}", file=sys.stderr)
            return 1
        print(f"restore drill passed: {res.summary()}")
        return 0
    kept = restore(settings, resolve(args.backup), stack_stopped=args.stack_stopped)
    print(
        f"restored {settings.db_path} from {args.backup}"
        + (f"; previous DB kept as {kept}" if kept else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
