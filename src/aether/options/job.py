"""Options snapshot job (`options`, daily 06:40 SGT; spec §6.8, §9). Research only.

Fetch every chain first (no transaction held), then one `write_tx`. A symbol whose fetch fails is
skipped and named in the run's warning; the others still store.

M8: each symbol's upcoming catalysts (with a known end date, within `max_days`) pick the extra
expiries the implied moves need; IV rank/percentile and volume vs median come from the symbol's
earlier snapshots.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select

from aether.config import OptionsConfig
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import catalysts, options_snapshots
from aether.db.types import utcnow_iso
from aether.options.analytics import IV_RANK_WINDOW, CatalystDate, history_metrics
from aether.options.snapshot import choose_expiries, compute
from aether.portfolio.holdings import universe_symbols
from aether.providers.options import YFinanceOptions
from aether.providers.prices import US_EASTERN, ProviderError
from aether.runs import JobResult


def upcoming_catalysts(
    engine: Engine, symbol: str, d: date, cfg: OptionsConfig
) -> list[CatalystDate]:
    """Upcoming catalysts on `symbol` with a known date (window end) within `max_days`."""
    horizon = (d + timedelta(days=cfg.max_days)).isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            select(catalysts.c.id, catalysts.c.title, catalysts.c.window_end)
            .where(
                catalysts.c.symbol == symbol,
                catalysts.c.status == "upcoming",
                catalysts.c.window_end.is_not(None),
                catalysts.c.window_end >= d.isoformat(),
                catalysts.c.window_end <= horizon,
            )
            .order_by(catalysts.c.window_end, catalysts.c.id)
        ).all()
    return [CatalystDate(r.id, r.title, date.fromisoformat(r.window_end)) for r in rows]


def past_snapshots(engine: Engine, symbol: str, d: date) -> list[tuple[float | None, int | None]]:
    """(atm_iv_30, total_volume) of the symbol's earlier snapshots, oldest first."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(options_snapshots.c.metrics)
            .where(options_snapshots.c.symbol == symbol, options_snapshots.c.d < d.isoformat())
            .order_by(options_snapshots.c.d.desc())
            .limit(IV_RANK_WINDOW)
        ).scalars()
        out = []
        for raw in rows:
            m: dict[str, Any] = json.loads(raw)
            out.append((m.get("atm_iv_30"), m.get("total_volume")))
    return out[::-1]


def snapshot_options(
    engine: Engine, provider: YFinanceOptions, cfg: OptionsConfig, d: date | None = None
) -> JobResult:
    d = d or datetime.now(US_EASTERN).date()  # the US session the snapshot belongs to
    rows, failed = [], []
    for sym in universe_symbols(engine):
        cats = upcoming_catalysts(engine, sym, d, cfg)
        dates = [c.d for c in cats]

        def choose(listed: list[date], dates: list[date] = dates) -> list[date]:
            return choose_expiries(listed, d, cfg, dates)

        try:
            chain = provider.fetch(sym, choose)
        except ProviderError as exc:
            failed.append(f"{sym}: {exc}"[:120])
            continue
        metrics, quality = compute(chain, d, cfg, cats)
        hist, why = history_metrics(
            metrics["atm_iv_30"], metrics["total_volume"], past_snapshots(engine, sym, d)
        )
        metrics.update(hist)
        quality["reasons"].update(why)
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
