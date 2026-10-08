"""`python -m aether.worker`: the single writer. Migrates, syncs config, runs the scheduler."""

from __future__ import annotations

import logging
import os

from sqlalchemy import Engine

from aether.classify.eval import sync_eval_results
from aether.config import Settings, get_settings, load_catalysts_config, load_watchlist
from aether.db import migrate
from aether.db.dialect import upsert
from aether.db.engine import ensure_db_file, make_rw_engine, write_tx
from aether.db.models import tickers
from aether.facts import load_facts, sync_facts
from aether.jobs import build_scheduler
from aether.logs import setup_logging
from aether.portfolio.holdings import import_positions_yaml

log = logging.getLogger("aether.worker")


def sync_config(engine: Engine, settings: Settings) -> None:
    watchlist = load_watchlist(settings.config_dir)
    facts = load_facts(settings.config_dir)
    # M8: a catalyst seed with an unknown fact id or symbol stops the worker here, not later.
    load_catalysts_config(
        settings.config_dir, {f.id for f in facts}, {t.symbol for t in watchlist.tickers}
    )
    # M14: every pure-play's modality must be backed by a fact in facts.yaml (spec §6.10).
    fact_ids = {f.id for f in facts}
    missing = sorted(
        f"{t.symbol}: {t.modality_fact}"
        for t in watchlist.tickers
        if t.modality_fact and t.modality_fact not in fact_ids
    )
    if missing:
        raise ValueError(f"watchlist modality_fact ids not in facts.yaml: {', '.join(missing)}")
    rows = [
        {
            "symbol": t.symbol,
            "type": t.type,
            "cik": t.cik,
            "active": int(t.active),
            "modality": t.modality,
            "sector": t.sector,
        }
        for t in watchlist.tickers
    ]
    with write_tx(engine) as conn:
        upsert(conn, tickers, rows, key_cols=["symbol"])
        sync_facts(conn, facts)
    evals = sync_eval_results(engine, settings.eval_results_dir)
    log.info("synced %d tickers, %d facts and %d new eval results", len(rows), len(facts), evals)


def startup(settings: Settings) -> Engine:
    os.umask(0o077)
    ensure_db_file(settings.db_path)
    migrate.upgrade(settings.db_path)
    engine = make_rw_engine(settings.db_path)
    sync_config(engine, settings)
    note = import_positions_yaml(engine, settings.config_dir)  # deprecated file, imported once
    if note:
        log.info(note)
    return engine


def main() -> None:
    settings = get_settings()
    setup_logging(settings)
    engine = startup(settings)
    log.info("worker started; db=%s", settings.db_path)
    build_scheduler(engine, settings).start()


if __name__ == "__main__":
    main()
