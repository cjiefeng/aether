"""`run_job`: every scheduled job runs through it and gets a `job_runs` row.

Kept apart from `jobs.py` (scheduler wiring) so ingest modules can import `JobResult` without a
cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Engine, insert, update

from aether.db.engine import write_tx
from aether.db.models import job_runs
from aether.db.types import utcnow_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobResult:
    rows_written: int = 0
    provider: str | None = None
    # Partial failure (e.g. one symbol of thirteen): the run is ok, the note goes in `error`.
    warning: str | None = None


def run_job(engine: Engine, name: str, fn: Callable[[], JobResult]) -> JobResult | None:
    with write_tx(engine) as conn:
        run_id = conn.execute(
            insert(job_runs)
            .values(job=name, started_at=utcnow_iso(), status="running")
            .returning(job_runs.c.id)
        ).scalar_one()
    try:
        result = fn()
    except Exception as exc:
        log.exception("job %s failed", name)
        with write_tx(engine) as conn:
            conn.execute(
                update(job_runs)
                .where(job_runs.c.id == run_id)
                .values(finished_at=utcnow_iso(), status="failed", error=repr(exc)[:500])
            )
        return None
    with write_tx(engine) as conn:
        conn.execute(
            update(job_runs)
            .where(job_runs.c.id == run_id)
            .values(
                finished_at=utcnow_iso(),
                status="ok",
                rows_written=result.rows_written,
                provider=result.provider,
                error=result.warning,
            )
        )
    return result
