"""M0 acceptance: one writer + two readers under WAL, no SQLITE_BUSY."""

from __future__ import annotations

import threading
import time

from sqlalchemy import Engine, func, insert, select

from aether.db.engine import make_ro_engine, write_tx
from aether.db.models import job_runs
from aether.db.types import utcnow_iso

DURATION_S = 3.0
BATCH = 50


def test_writer_and_two_readers_no_busy(rw_engine: Engine, migrated_db: object) -> None:
    errors: list[BaseException] = []
    reader_counts: dict[int, list[int]] = {0: [], 1: []}
    stop = threading.Event()
    written = 0

    def writer() -> None:
        nonlocal written
        try:
            deadline = time.monotonic() + DURATION_S
            while time.monotonic() < deadline:
                rows = [
                    {"job": "concurrency", "started_at": utcnow_iso(), "status": "ok"}
                    for _ in range(BATCH)
                ]
                with write_tx(rw_engine) as conn:
                    conn.execute(insert(job_runs), rows)
                written += BATCH
        except BaseException as exc:
            errors.append(exc)
        finally:
            stop.set()

    def reader(idx: int) -> None:
        engine = make_ro_engine(rw_engine.url.database)  # type: ignore[arg-type]
        try:
            while not stop.is_set():
                with engine.connect() as conn:
                    n = conn.execute(select(func.count()).select_from(job_runs)).scalar_one()
                reader_counts[idx].append(int(n))
        except BaseException as exc:
            errors.append(exc)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader, args=(i,)) for i in (0, 1)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=DURATION_S + 10)

    assert not errors, errors
    assert written >= BATCH * 10
    for counts in reader_counts.values():
        assert len(counts) > 10, "reader made too few reads"
        assert counts == sorted(counts), "reader saw the count go backwards"
        assert all(c % BATCH == 0 for c in counts), "reader saw a partial batch"
