#!/usr/bin/env python3
"""M15 cache A/B for the research sweep (costs real money: two Opus web-search calls, ~$0.4).

Runs the sweep request for one watchlist ticker twice through the LLM wrapper, logged with
purpose `research_eval`: first with no cache marker, then with the system-prefix marker. Prints
each arm's usage and cost and appends both rows to exports/llm-dev-log.csv (the temp DB is
thrown away).

    make sweep-ab SYMBOL=ACME
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from aether.config import get_settings, load_llm_config, load_sources, load_watchlist
from aether.db import migrate
from aether.db.engine import ensure_db_file, make_rw_engine
from aether.db.models import llm_calls
from aether.llm.client import DEV_LOG_PATH, RESEARCH_EVAL_PURPOSE, LlmClient, web_search_tool
from aether.ops.llm_log import append_dev_log
from aether.research.runner import SYSTEM_PROMPT, Target, build_prompt

ARMS = (("cache_off", False), ("cache_on", True))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol")
    args = ap.parse_args(argv)
    settings = get_settings()
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (make sweep-ab reads it from .env).", file=sys.stderr)
        return 2
    cfg = load_llm_config(settings.config_dir)
    sources = load_sources(settings.config_dir)
    wl = {t.symbol: t for t in load_watchlist(settings.config_dir).tickers}
    t = wl.get(args.symbol)
    if t is None:
        print(f"{args.symbol!r} is not in the watchlist", file=sys.stderr)
        return 2
    target = Target(t.symbol, t.aliases[0] if t.aliases else t.symbol)
    end = datetime.now(UTC).date()
    start = end - timedelta(days=cfg.research.sweep_days - 1)
    model = settings.research_model
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "ab.db"
        ensure_db_file(db)
        migrate.upgrade(db)
        engine = make_rw_engine(db)
        llm = LlmClient(engine, settings.model_copy(update={"db_path": db}), cfg)
        for arm, cache in ARMS:
            llm.complete(
                purpose=RESEARCH_EVAL_PURPOSE,
                model=model,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_prompt(target, start, end)}],
                max_tokens=cfg.research.max_tokens,
                tools=[web_search_tool(cfg.research.sweep_max_uses, sources.allowed_domains())],
                effort=cfg.research.effort,
                context={"symbol": target.symbol, "kind": "sweep_ab", "arm": arm},
                cache_prefix=cache,
            )
        with engine.connect() as conn:
            for r in conn.execute(select(llm_calls).order_by(llm_calls.c.id)).all():
                print(
                    f"{r.context}: status={r.status} input={r.input_tokens} "
                    f"cache_write={r.cache_write_tokens} cache_read={r.cache_read_tokens} "
                    f"output={r.output_tokens} searches={r.web_searches} cost=${r.cost_micros}"
                )
        n = append_dev_log(engine, Path(DEV_LOG_PATH))
        engine.dispose()
    print(f"{n} rows appended to {DEV_LOG_PATH} ({model}, window {start} to {end})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
