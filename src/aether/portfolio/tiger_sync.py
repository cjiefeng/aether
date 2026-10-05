"""Tiger holdings sync (spec §1.3, S8; M5). Worker only.

Replaces the share counts of strategy-universe symbols (QTUM + pure-plays) with the account's
positions. Positions outside the universe are ignored (only their count is kept), and the
sleeve's cash stays a manual entry. A failed sync changes no holdings: the last snapshot stays
in use and the dashboard shows "stale since <last successful sync>".

Network first (no transaction held), then one short `write_tx`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Engine

from aether.db.engine import write_tx
from aether.db.types import utcnow_iso
from aether.portfolio.holdings import (
    load_settings,
    read_settings,
    replace_universe_positions,
    set_setting,
    universe_symbols,
)
from aether.providers.tiger import TigerError, TigerReadOnly
from aether.runs import JobResult


def _record_failure(engine: Engine, error: str) -> None:
    now = utcnow_iso()
    with write_tx(engine) as conn:
        prev = read_settings(conn).tiger_sync or {}
        set_setting(conn, "tiger_sync", {**prev, "last_error_at": now, "error": error}, now)


def sync_holdings(
    engine: Engine,
    tiger: TigerReadOnly | None,
    disabled_reason: str | None,
    command_id: int | None = None,
) -> JobResult:
    """Run one sync. Skipped unless `holdings_source` is `tiger`, so a sync (scheduled or from
    the dashboard's button) never overwrites manual rows."""
    settings = load_settings(engine)
    if settings.holdings_source != "tiger":
        return JobResult(warning="holdings_source is manual; Tiger sync skipped")
    if tiger is None:
        reason = disabled_reason or "Tiger not configured"
        _record_failure(engine, reason)
        raise TigerError(reason)
    try:
        positions = tiger.positions()
    except TigerError as exc:
        _record_failure(engine, str(exc))
        raise
    universe = set(universe_symbols(engine))
    inside: dict[str, tuple[Decimal, Decimal | None]] = {
        p.symbol: (p.quantity, p.average_cost)
        for p in positions
        if p.symbol in universe and p.quantity > 0
    }
    outside = len({p.symbol for p in positions if p.symbol not in universe})
    now = utcnow_iso()
    with write_tx(engine) as conn:
        replace_universe_positions(conn, inside, "tiger", command_id, now)
        status: dict[str, Any] = {
            "last_ok": now,
            "last_error_at": None,
            "error": None,
            "universe_positions": len(inside),
            "outside_universe": outside,
            "account_masked": tiger.account_masked,
        }
        set_setting(conn, "tiger_sync", status, now)
    return JobResult(rows_written=len(inside), provider="tiger")
