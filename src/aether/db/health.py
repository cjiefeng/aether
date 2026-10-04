"""Read-only queries for the health page and container healthchecks."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from aether.db.models import job_runs


@dataclass(frozen=True)
class JobStatus:
    job: str
    status: str
    started_at: str
    finished_at: str | None
    provider: str | None
    error: str | None


@dataclass(frozen=True)
class DbHealth:
    ok: bool
    sqlite_version: str | None = None
    journal_mode: str | None = None
    schema_revision: str | None = None
    file_mode: str | None = None
    error: str | None = None
    jobs: tuple[JobStatus, ...] = field(default_factory=tuple)


def file_mode(path: Path) -> str | None:
    try:
        return oct(stat.S_IMODE(os.stat(path).st_mode))
    except OSError:
        return None


def collect(engine: Engine, db_path: Path) -> DbHealth:
    try:
        with engine.connect() as conn:
            version = conn.execute(select(func.sqlite_version())).scalar_one()
            journal = conn.execute(text("PRAGMA journal_mode")).scalar_one()
            revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            latest = select(func.max(job_runs.c.id).label("id")).group_by(job_runs.c.job).subquery()
            rows = conn.execute(
                select(job_runs).join(latest, job_runs.c.id == latest.c.id).order_by(job_runs.c.job)
            ).all()
    except SQLAlchemyError as exc:
        return DbHealth(ok=False, file_mode=file_mode(db_path), error=type(exc).__name__)
    jobs = tuple(
        JobStatus(r.job, r.status, r.started_at, r.finished_at, r.provider, r.error) for r in rows
    )
    return DbHealth(
        ok=journal == "wal",
        sqlite_version=str(version),
        journal_mode=str(journal),
        schema_revision=revision,
        file_mode=file_mode(db_path),
        jobs=jobs,
    )
