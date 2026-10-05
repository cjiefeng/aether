"""Read-only options panel data (ticker page, review pack; spec §6.8). Research only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select

from aether.db.models import options_snapshots
from aether.market import job_stale, last_ok_finished

OPTIONS_JOB_MAX_AGE = timedelta(hours=36)


def options_stale(engine: Engine, now: datetime | None = None) -> tuple[str | None, bool]:
    last = last_ok_finished(engine, "options")
    return last, job_stale(last, now or datetime.now(UTC), OPTIONS_JOB_MAX_AGE)


def options_panel(engine: Engine, symbols: list[str]) -> list[dict[str, Any]]:
    """The latest options snapshot per name: summary metrics and quality flags only."""
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for sym in symbols:
            row = conn.execute(
                select(
                    options_snapshots.c.d,
                    options_snapshots.c.metrics,
                    options_snapshots.c.quality,
                    options_snapshots.c.provider,
                )
                .where(options_snapshots.c.symbol == sym)
                .order_by(options_snapshots.c.d.desc())
                .limit(1)
            ).first()
            if row is None:
                out.append({"symbol": sym, "d": None})
                continue
            m, q = json.loads(row.metrics), json.loads(row.quality)
            out.append(
                {
                    "symbol": sym,
                    "d": row.d,
                    "provider": row.provider,
                    "thin": bool(q.get("thin")),
                    "atm_iv_30": m.get("atm_iv_30"),
                    "term": m.get("term", {}),
                    "skew_30": m.get("skew_30"),
                    "iv_rank": m.get("iv_rank"),
                    "iv_percentile": m.get("iv_percentile"),
                    "iv_history_days": m.get("iv_history_days"),
                    "put_call_volume": m.get("put_call_volume"),
                    "put_call_oi": m.get("put_call_oi"),
                    "volume_vs_median": m.get("volume_vs_median"),
                    "implied_moves": m.get("implied_moves", []),
                    "reasons": q.get("reasons", {}),
                }
            )
    return out
