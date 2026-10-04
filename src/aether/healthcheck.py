"""Container healthchecks: `python -m aether.healthcheck worker|app`. Exit 0 means healthy."""

from __future__ import annotations

import sys
import urllib.request
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from aether.config import get_settings
from aether.db import migrate
from aether.db.engine import make_ro_engine
from aether.db.models import job_runs
from aether.db.types import to_iso
from aether.jobs import HEARTBEAT_MINUTES


def check_worker() -> bool:
    settings = get_settings()
    if not settings.db_path.exists():
        return False
    engine = make_ro_engine(settings.db_path)
    try:
        if migrate.current_revision(engine) != migrate.head_revision():
            return False
        cutoff = to_iso(datetime.now(UTC) - timedelta(minutes=3 * HEARTBEAT_MINUTES))
        with engine.connect() as conn:
            n = conn.execute(
                select(func.count())
                .select_from(job_runs)
                .where(
                    job_runs.c.job == "heartbeat",
                    job_runs.c.status == "ok",
                    job_runs.c.started_at >= cutoff,
                )
            ).scalar_one()
        return int(n) > 0
    finally:
        engine.dispose()


def check_app() -> bool:
    _, port = get_settings().bind_host_port
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as resp:
        return bool(resp.status == 200)


def main(argv: list[str]) -> int:
    checks = {"worker": check_worker, "app": check_app}
    if len(argv) != 1 or argv[0] not in checks:
        print("usage: python -m aether.healthcheck worker|app", file=sys.stderr)
        return 2
    try:
        return 0 if checks[argv[0]]() else 1
    except Exception as exc:
        print(f"unhealthy: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
