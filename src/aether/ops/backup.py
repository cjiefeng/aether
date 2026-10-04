"""Nightly online backup (spec §2.1): `data/backups/aether-YYYYMMDD.db`, 14 days kept, mode 0600.

Uses `sqlite3.Connection.backup()`, the same online, consistent API that the CLI's `.backup`
uses. The slim runtime image has no sqlite3 binary.

    python -m aether.ops.backup
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^aether-(\d{8})\.db$")


def backup(
    db_path: Path, dest_dir: Path, *, keep_days: int = 14, today: date | None = None
) -> Path:
    today = today or datetime.now(UTC).date()
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    final = dest_dir / f"aether-{today:%Y%m%d}.db"
    tmp = final.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    os.close(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        # A backup copy is a standalone file; rollback journal avoids stray -wal/-shm files.
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()
    os.chmod(tmp, 0o600)
    tmp.replace(final)
    prune(dest_dir, keep_days=keep_days, today=today)
    log.info("backup written to %s", final)
    return final


def prune(dest_dir: Path, *, keep_days: int, today: date) -> list[Path]:
    cutoff = today - timedelta(days=keep_days - 1)
    removed = []
    for p in dest_dir.iterdir():
        m = _NAME_RE.match(p.name)
        if m and datetime.strptime(m.group(1), "%Y%m%d").date() < cutoff:
            p.unlink()
            removed.append(p)
    return removed


def main() -> int:
    from aether.config import get_settings

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    print(backup(s.db_path, s.resolved_backup_dir, keep_days=s.backup_keep_days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
