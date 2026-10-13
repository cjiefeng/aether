"""M15 (spec §9): weekly research sweep and portfolio jobs, the 8-day startup catch-up and stale
tolerance, and the Sunday ordering (conclusions wait for classification, up to 60 minutes)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert

from aether.db.engine import write_tx
from aether.db.models import job_runs
from aether.db.types import to_iso
from aether.jobs import WEEKLY_CATCH_UP_AFTER, _catch_up, build_scheduler, wait_for_classification
from aether.market import WEEKLY_JOB_MAX_AGE, job_stale
from aether.news_view import sweep_stale
from tests.conftest import make_settings
from tests.llm_fakes import llm_settings


def _ran(engine: Engine, job: str, days_ago: float) -> str:
    at = to_iso(datetime.now(UTC) - timedelta(days=days_ago))
    with write_tx(engine) as conn:
        conn.execute(insert(job_runs).values(job=job, started_at=at, finished_at=at, status="ok"))
    return at


def test_weekly_jobs_are_sunday_crons(rw_engine: Engine, migrated_db: Path) -> None:
    sched = build_scheduler(rw_engine, make_settings(migrated_db))
    for jid, hour, minute in (("research_sweep", "7", "0"), ("portfolio", "7", "10")):
        fields = {f.name: str(f) for f in sched.get_job(jid).trigger.fields}
        assert (fields["day_of_week"], fields["hour"], fields["minute"]) == ("sun", hour, minute)


def test_sweep_catch_up_only_after_8_days(rw_engine: Engine) -> None:
    _ran(rw_engine, "research_sweep", 2)
    assert _catch_up(rw_engine, "research_sweep", after=WEEKLY_CATCH_UP_AFTER) == {}
    _ran(rw_engine, "portfolio_probe", 9)
    assert "next_run_time" in _catch_up(rw_engine, "portfolio_probe", after=WEEKLY_CATCH_UP_AFTER)


def test_restart_2_days_after_sweep_runs_none_9_days_runs_one(
    rw_engine: Engine, migrated_db: Path
) -> None:
    """With an API key the scheduler adds a startup catch-up only for a 9-day-old sweep."""
    _ran(rw_engine, "research_sweep", 2)
    sched = build_scheduler(rw_engine, llm_settings(migrated_db, research_backfill=False))
    # APScheduler 3: a job added with an explicit `next_run_time` (the catch-up) has it set before
    # the scheduler starts; a cron-only job gets it at start.
    assert not hasattr(sched.get_job("research_sweep"), "next_run_time")
    with write_tx(rw_engine) as conn:
        conn.execute(job_runs.delete().where(job_runs.c.job == "research_sweep"))
    _ran(rw_engine, "research_sweep", 9)
    sched = build_scheduler(rw_engine, llm_settings(migrated_db, research_backfill=False))
    assert sched.get_job("research_sweep").next_run_time is not None


def test_weekly_stale_tolerance(rw_engine: Engine) -> None:
    now = datetime.now(UTC)
    assert timedelta(days=8) == WEEKLY_JOB_MAX_AGE
    assert not job_stale(to_iso(now - timedelta(days=7)), now, WEEKLY_JOB_MAX_AGE)
    assert job_stale(to_iso(now - timedelta(days=9)), now, WEEKLY_JOB_MAX_AGE)
    assert sweep_stale(rw_engine) is None  # never ran: no banner
    _ran(rw_engine, "research_sweep", 9)
    assert (sweep_stale(rw_engine) or "").startswith("Weekly research sweep stale since")


def test_conclusions_wait_for_classification(
    monkeypatch: pytest.MonkeyPatch, rw_engine: Engine
) -> None:
    import aether.jobs as jobs

    t = {"now": 0.0, "checks": 0}
    busy_until = {"n": 3}

    def work(_engine: Engine, *, batches_only: bool = False) -> bool:
        if batches_only:
            return False
        t["checks"] += 1
        return t["checks"] <= busy_until["n"]

    def sleep(s: float) -> None:
        t["now"] += s

    monkeypatch.setattr(jobs, "has_classifier_work", work)
    assert wait_for_classification(rw_engine, sleep=sleep, clock=lambda: t["now"]) is True
    assert t["now"] == 3 * jobs.CONCLUSIONS_POLL_S
    # Still busy after 60 minutes: give up and let conclusions run.
    t.update(now=0.0, checks=0)
    busy_until["n"] = 10_000
    assert wait_for_classification(rw_engine, sleep=sleep, clock=lambda: t["now"]) is False
    assert t["now"] == timedelta(minutes=60).total_seconds()
