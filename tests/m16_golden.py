"""M16 no-regression golden (spec §11): Safe and Medium targets must be byte-identical before and
after the pure-play tilt. The fixture was generated from the M15 code on `main` (2b31e28) with

    docker compose run --rm -T dev uv run --frozen python -m tests.m16_golden

Synthetic panel only (ACME… pure-plays, ADJA… adjacent names; seeded RNG)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aether.config import load_strategies
from aether.portfolio.job import Inputs, canon, compute_run
from aether.portfolio.overlay import Finding, StanceAdj, apply_overlay
from tests.conftest import CONFIG_DIR
from tests.portfolio_data import panel, random_closes, weekdays

FIXTURE = Path(__file__).parent / "fixtures" / "m16_safe_medium_golden.json"
PURE = ("ACME", "DEMO", "EXMP", "FAKE")
ADJACENT = ("ADJA", "ADJB", "ADJC")
PROFILES = ("safe", "medium")


def inputs(n: int = 260) -> Inputs:
    closes = panel(n)
    days = weekdays(n)
    for i, s in enumerate(ADJACENT):
        closes[s] = list(zip(days, random_closes(n, seed=500 + i, vol=0.012), strict=True))
    return Inputs(tuple(sorted((*PURE, *ADJACENT))), closes)


def payload() -> dict[str, Any]:
    cfg = load_strategies(CONFIG_DIR)
    out = compute_run(inputs(), cfg)
    assert out is not None
    sleeve = sorted((*PURE, *ADJACENT))
    cut = Finding("ACME", "going_concern", 0.0, "0000000000-26-000001", "10-K", "2025-06-01", "", 1)
    up = StanceAdj("ADJA", "ACCUMULATE", 1.25, 1.25, False, 7, "2025-06-01", "proven")
    res: dict[str, Any] = {}
    for p in PROFILES:
        sel = out.summary["profiles"][p]
        sid = sel["strategy_id"]
        base = out.weights[sid]
        pp = cfg.profiles[p]  # type: ignore[index]
        published, chain = apply_overlay(base, sleeve, pp.max_per_name, [cut], [up])
        res[p] = {
            "strategy_id": sid,
            "ranking": sel["ranking"],
            "weights": {s: w for s, w in out.weights.items() if s.startswith(f"{p}:")},
            "published": published,
            "chain": chain,
        }
    return json.loads(canon(res))


def write() -> None:
    FIXTURE.write_text(json.dumps(payload(), indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write()
