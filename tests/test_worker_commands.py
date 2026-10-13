"""The dashboard → worker command contract, checked against the handlers `build_scheduler`
actually wires up (not a test-only copy)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from aether import jobs
from aether.db.commands import ALLOWED_KINDS
from aether.runs import JobResult
from tests.conftest import make_settings


def worker_handlers(engine: Engine, db: Path) -> dict[str, Any]:
    job = jobs.build_scheduler(engine, make_settings(db)).get_job("process_commands")
    assert job is not None
    return dict(job.kwargs["handlers"])


def test_every_allowed_command_has_a_worker_handler(rw_engine: Engine, migrated_db: Path) -> None:
    """A kind the dashboard can enqueue but the worker can't handle would be `rejected` at
    runtime; a handler with no allowed kind is dead code."""
    assert set(worker_handlers(rw_engine, migrated_db)) == ALLOWED_KINDS


def _hold_open(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> tuple[threading.Event, threading.Event]:
    """Replace `jobs.<target>` with a job that blocks until released."""
    started, release = threading.Event(), threading.Event()

    def blocking(*_a: object, **_k: object) -> JobResult:
        started.set()
        assert release.wait(5), "test never released the job"
        return JobResult(rows_written=3)

    monkeypatch.setattr(jobs, target, blocking)
    return started, release


@pytest.mark.parametrize(
    ("kind", "blocking_job", "stubs"),
    [
        ("recompute_strategies", "strategies_job", ("ingest_dividends", "rebalance_job")),
        ("refresh_prices", "prices_job", ()),
    ],
)
def test_second_request_while_running_reports_busy(
    rw_engine: Engine,
    migrated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    blocking_job: str,
    stubs: tuple[str, ...],
) -> None:
    """The dashboard command and the scheduled job share a lock: a second request returns
    `busy` at once instead of queueing a duplicate run (or waiting on the network)."""
    for name in stubs:
        monkeypatch.setattr(jobs, name, lambda *_a, **_k: JobResult())
    started, release = _hold_open(monkeypatch, blocking_job)
    handler = worker_handlers(rw_engine, migrated_db)[kind]

    first: dict[str, Any] = {}
    t = threading.Thread(target=lambda: first.update(handler({})))
    t.start()
    try:
        assert started.wait(5)
        assert handler({}) == {"ok": False, "busy": True}
    finally:
        release.set()
        t.join(5)
    assert first == {"ok": True, "rows": 3}
