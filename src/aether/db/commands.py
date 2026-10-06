"""The `commands` queue: the dashboard requests work, the worker executes it.

`enqueue_command` is the dashboard's ONLY write path into SQLite. It runs on the command
engine, whose SQLite authorizer permits nothing but INSERT INTO commands.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, Row, func, insert, select

from aether.db.engine import write_tx
from aether.db.models import commands
from aether.db.types import to_iso, utcnow_iso

# Allow-list of command kinds the dashboard may request. Extended per milestone.
ALLOWED_KINDS = frozenset(
    {
        "ping",
        "refresh_prices",
        "refresh_edgar",
        "test_alert",
        "recompute_strategies",
        # M5: owner data (holdings, portfolio settings) and the Tiger sync.
        "update_holdings",
        "update_portfolio_settings",
        "sync_holdings",
        "publish_targets",
        # M6: an on-demand research sweep (LLM spend; budget-guarded in the worker).
        "research_sweep",
        # M8: the owner marks a catalyst hit / slipped / cancelled, or reopens it.
        "mark_catalyst",
    }
)


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


# A command still pending/running after this long is treated as stuck (worker down or crashed
# mid-run): the dashboard stops waiting on it and allows a new request of the same kind.
ACTIVE_WINDOW = timedelta(hours=1)


def get_command(ro_engine: Engine, command_id: int) -> Row[Any] | None:
    with ro_engine.connect() as conn:
        return conn.execute(select(commands).where(commands.c.id == command_id)).first()


def active_command(ro_engine: Engine, kinds: Sequence[str]) -> Row[Any] | None:
    """The newest pending/running command of one of `kinds`, requested within ACTIVE_WINDOW."""
    cutoff = to_iso(datetime.now(UTC) - ACTIVE_WINDOW)
    with ro_engine.connect() as conn:
        return conn.execute(
            select(commands)
            .where(
                commands.c.kind.in_(list(kinds)),
                commands.c.status.in_(("pending", "running")),
                commands.c.requested_at >= cutoff,
            )
            .order_by(commands.c.id.desc())
            .limit(1)
        ).first()


def rate_limit_resets_at(ro_engine: Engine, window: timedelta = timedelta(hours=1)) -> datetime:
    """When the oldest command in the trailing window drops out of it (a slot frees up)."""
    now = datetime.now(UTC)
    with ro_engine.connect() as conn:
        oldest = conn.execute(
            select(func.min(commands.c.requested_at)).where(
                commands.c.requested_at >= to_iso(now - window)
            )
        ).scalar_one()
    if oldest is None:
        return now
    return datetime.fromisoformat(oldest) + window
