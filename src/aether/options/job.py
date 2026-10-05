"""Options snapshot job (`options`, daily 06:40 SGT; spec §6.8, §9). Research only.

Fetch every chain first (no transaction held), then one `write_tx`. A symbol whose fetch fails is
skipped and named in the run's warning; the others still store.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import Engine

from aether.config import OptionsConfig
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import options_snapshots
from aether.db.types import utcnow_iso
from aether.options.snapshot import choose_expiries, compute
from aether.portfolio.holdings import universe_symbols
from aether.providers.options import YFinanceOptions
from aether.providers.prices import US_EASTERN, ProviderError
from aether.runs import JobResult


def snapshot_options(
    engine: Engine, provider: YFinanceOptions, cfg: OptionsConfig, d: date | None = None
) -> JobResult:
    d = d or datetime.now(US_EASTERN).date()  # the US session the snapshot belongs to
    rows, failed = [], []
    for sym in universe_symbols(engine):
        try:
            chain = provider.fetch(sym, lambda listed: choose_expiries(listed, d, cfg))
        except ProviderError as exc:
            failed.append(f"{sym}: {exc}"[:120])
            continue
        metrics, quality = compute(chain, d, cfg)
        rows.append(
            {
                "symbol": sym,
                "d": d.isoformat(),
                "metrics": json.dumps(metrics, sort_keys=True),
                "quality": json.dumps(quality, sort_keys=True),
                "provider": provider.name,
                "fetched_at": utcnow_iso(),
            }
        )
    if rows:
        with write_tx(engine) as conn:
            upsert(conn, options_snapshots, rows, key_cols=["symbol", "d"])
    if not rows and failed:
        raise ProviderError("; ".join(failed)[:500])
    return JobResult(
        rows_written=len(rows),
        provider=provider.name,
        warning="; ".join(failed)[:500] if failed else None,
    )
