"""Container healthchecks (`python -m aether.healthcheck worker|app`): what `docker compose up
--wait` and `./deploy.sh` gate on."""

from __future__ import annotations

import io
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, insert

from aether import healthcheck
from aether.db import migrate
from aether.db.engine import ensure_db_file, make_rw_engine, write_tx
from aether.db.models import job_runs
from aether.db.types import to_iso
from aether.jobs import HEARTBEAT_MINUTES
from tests.conftest import make_settings


@pytest.fixture
def use_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _use(path: Path) -> None:
        monkeypatch.setattr(healthcheck, "get_settings", lambda: make_settings(path))

    return _use


def beat(engine: Engine, minutes_ago: float, status: str = "ok") -> None:
    started = to_iso(datetime.now(UTC) - timedelta(minutes=minutes_ago))
    with write_tx(engine) as conn:
        conn.execute(
            insert(job_runs).values(
                job="heartbeat", started_at=started, finished_at=started, status=status
            )
        )


def test_worker_unhealthy_without_db(tmp_path: Path, use_db: Any) -> None:
    use_db(tmp_path / "missing.db")
    assert healthcheck.check_worker() is False


def test_worker_unhealthy_without_heartbeat(
    rw_engine: Engine, migrated_db: Path, use_db: Any
) -> None:
    use_db(migrated_db)
    assert healthcheck.check_worker() is False


def test_worker_healthy_with_recent_heartbeat(
    rw_engine: Engine, migrated_db: Path, use_db: Any
) -> None:
    use_db(migrated_db)
    beat(rw_engine, minutes_ago=1)
    assert healthcheck.check_worker() is True
    assert healthcheck.main(["worker"]) == 0


@pytest.mark.parametrize(
    ("minutes_ago", "status"),
    [(3 * HEARTBEAT_MINUTES + 1, "ok"), (1, "failed"), (1, "running")],
)
def test_worker_unhealthy_with_stale_or_failed_heartbeat(
    rw_engine: Engine, migrated_db: Path, use_db: Any, minutes_ago: float, status: str
) -> None:
    use_db(migrated_db)
    beat(rw_engine, minutes_ago=minutes_ago, status=status)
    assert healthcheck.check_worker() is False
    assert healthcheck.main(["worker"]) == 1


def test_worker_unhealthy_when_schema_is_behind_head(tmp_path: Path, use_db: Any) -> None:
    """A deploy whose migration hasn't run yet must not report healthy, even with a heartbeat."""
    head = ScriptDirectory.from_config(migrate.alembic_config()).get_revision("head")
    assert isinstance(head.down_revision, str)
    path = tmp_path / "behind.db"
    ensure_db_file(path)
    migrate.upgrade(path, head.down_revision)
    engine = make_rw_engine(path)
    try:
        beat(engine, minutes_ago=1)
    finally:
        engine.dispose()
    use_db(path)
    assert healthcheck.check_worker() is False


class _Resp(io.BytesIO):
    def __init__(self, status: int) -> None:
        super().__init__(b"{}")
        self.status = status


def test_app_check_follows_healthz(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    monkeypatch.setattr(healthcheck, "get_settings", lambda: settings)
    seen: list[str] = []

    def ok(url: str, timeout: float) -> _Resp:
        seen.append(url)
        return _Resp(200)

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", ok)
    assert healthcheck.main(["app"]) == 0
    assert seen == [f"http://127.0.0.1:{settings.bind_host_port[1]}/healthz"]

    def unavailable(url: str, timeout: float) -> _Resp:
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", unavailable)
    assert healthcheck.main(["app"]) == 1


def test_usage_errors() -> None:
    assert healthcheck.main([]) == 2
    assert healthcheck.main(["nope"]) == 2
