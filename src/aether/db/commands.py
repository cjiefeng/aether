"""The `commands` queue: the dashboard requests work, the worker executes it.

`enqueue_command` is the dashboard's ONLY write path into SQLite. It runs on the command
engine, whose SQLite authorizer permits nothing but INSERT INTO commands.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, insert, select

from aether.db.engine import write_tx
from aether.db.models import commands
from aether.db.types import to_iso, utcnow_iso

# Allow-list of command kinds the dashboard may request. Extended per milestone.
ALLOWED_KINDS = frozenset({"ping", "refresh_prices"})


class UnknownCommandError(ValueError):
    pass


def enqueue_command(
    command_engine: Engine, kind: str, args: dict[str, Any], requested_by: str
) -> int:
    if kind not in ALLOWED_KINDS:
        raise UnknownCommandError(kind)
    with write_tx(command_engine) as conn:
        new_id = conn.execute(
            insert(commands)
            .values(
                kind=kind,
                args=json.dumps(args, sort_keys=True),
                requested_at=utcnow_iso(),
                requested_by=requested_by,
                status="pending",
            )
            .returning(commands.c.id)
        ).scalar_one()
    return int(new_id)


def count_recent_commands(ro_engine: Engine, window: timedelta = timedelta(hours=1)) -> int:
    """Commands requested in the trailing window (rate limit; survives app restarts)."""
    cutoff = to_iso(datetime.now(UTC) - window)
    with ro_engine.connect() as conn:
        n = conn.execute(
            select(func.count()).select_from(commands).where(commands.c.requested_at >= cutoff)
        ).scalar_one()
    return int(n)
