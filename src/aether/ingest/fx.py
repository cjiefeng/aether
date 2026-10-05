"""USD/SGD ingest (job `fx`, daily 06:50 SGT; spec §9). Reporting only. Network first, then one
short `write_tx`."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Engine

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import fx_rates
from aether.db.types import utcnow_iso
from aether.providers.fx import PAIR, FxProvider, default_window
from aether.runs import JobResult


def ingest_fx(engine: Engine, provider: FxProvider, today: date | None = None) -> JobResult:
    start, end = default_window(today or date.today())
    rates = provider.fetch(start, end)
    if not rates:
        return JobResult(warning="no USD/SGD rates returned")
    now = utcnow_iso()
    rows = [
        {
            "pair": PAIR,
            "d": r.d.isoformat(),
            "rate": r.rate,
            "provider": r.provider,
            "fetched_at": now,
        }
        for r in rates
    ]
    with write_tx(engine) as conn:
        upsert(conn, fx_rates, rows, key_cols=["pair", "d"])
    return JobResult(rows_written=len(rows), provider=rates[-1].provider)
