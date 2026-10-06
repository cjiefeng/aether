"""M10 acceptance: research overlay layer 2 (spec §6.6.1). An unproven ticker's AVOID gives
x 0.75; a proven one's gives x 0. Stances never squeeze QTUM's fixed weight. Synthetic data."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import Engine, select

from aether.config import load_strategies, load_weights
from aether.db.models import profile_targets
from aether.portfolio.overlay import CORE, StanceAdj, apply_overlay, chain_text
from aether.portfolio.publish import publish_targets
from tests.conclusions_data import add_conclusion, add_outcome
from tests.conftest import CONFIG_DIR
from tests.holdings_data import fake_run, seed_prices

CONFIG = load_strategies(CONFIG_DIR)
W = load_weights(CONFIG_DIR)
SLEEVE = ("ACME", "DEMO", "EXMP", "FAKE")
BASE = {"QTUM": 0.45, "ACME": 0.20, "DEMO": 0.15, "EXMP": 0.10, "FAKE": 0.10}


def adj(sym: str, stance: str, applied: float, raw: float | None = None) -> StanceAdj:
    raw = CONFIG.overlay.stance_multipliers[stance] if raw is None else raw
    return StanceAdj(sym, stance, raw, applied, applied != raw, 11, "2026-09-27", "t")


def test_unproven_avoid_is_clamped_and_freed_weight_stays_in_the_sleeve() -> None:
    pub, chain = apply_overlay(BASE, SLEEVE, 0.20, [], [adj("DEMO", "AVOID", 0.75)])
    assert sum(pub.values()) == pytest.approx(1.0)
    assert pub["QTUM"] == pytest.approx(0.45)
    # DEMO 15% x 0.75 = 11.25%; the 3.75% freed goes to EXMP and FAKE (ACME is at its cap).
    assert pub["DEMO"] == pytest.approx(0.1125)
    assert pub["ACME"] == pytest.approx(0.20)
    assert pub["EXMP"] == pytest.approx(0.10 + 0.0375 / 2)
    demo = next(c for c in chain if c["symbol"] == "DEMO")
    assert demo["steps"][0]["label"] == "AVOID × 0 → clamped × 0.75 (unproven)"  # noqa: RUF001
    assert "conclusion #11" in chain_text(demo)


def test_proven_avoid_zeroes_the_name() -> None:
    pub, _ = apply_overlay(BASE, SLEEVE, 0.20, [], [adj("DEMO", "AVOID", 0.0)])
    assert "DEMO" not in pub and pub["QTUM"] == pytest.approx(0.45)
    assert sum(pub.values()) == pytest.approx(1.0)


def test_accumulate_is_relative_and_never_squeezes_qtum() -> None:
    pub, chain = apply_overlay(BASE, SLEEVE, 0.20, [], [adj("EXMP", "ACCUMULATE", 1.25)])
    assert pub["QTUM"] == pytest.approx(0.45)
    assert sum(pub[s] for s in SLEEVE) == pytest.approx(0.55)
    assert pub["EXMP"] / pub["FAKE"] == pytest.approx(1.25)
    # Everyone ACCUMULATE -> no change at all.
    every = [adj(s, "ACCUMULATE", 1.25) for s in SLEEVE]
    pub2, _ = apply_overlay(BASE, SLEEVE, 0.20, [], every)
    assert pub2 == pytest.approx(BASE)
    # A cap is never exceeded because of a stance.
    pub3, _ = apply_overlay(BASE, SLEEVE, 0.20, [], [adj("ACME", "ACCUMULATE", 1.25)])
    assert pub3["ACME"] == pytest.approx(0.20) and pub3["QTUM"] == pytest.approx(0.45)
    assert any(c["steps"] for c in chain)


def published(engine: Engine, day: str, profile: str = "medium") -> tuple[dict, dict]:
    with engine.connect() as conn:
        r = conn.execute(
            select(profile_targets).where(
                profile_targets.c.profile == profile, profile_targets.c.as_of == day
            )
        ).one()
    return json.loads(r.published_weights), json.loads(r.adjustments)


def test_publish_applies_stances_with_the_earned_trust_clamp(rw_engine: Engine) -> None:
    seed_prices(rw_engine, n=200, start=date(2026, 1, 2))
    fake_run(rw_engine, "2026-09-30", {"medium": BASE})
    add_conclusion(rw_engine, "DEMO", "2026-09-27", "AVOID")
    publish_targets(rw_engine, CONFIG, today=date(2026, 10, 1), weights=W)
    w, adjm = published(rw_engine, "2026-10-01")
    assert w["DEMO"] == pytest.approx(0.15 * 0.75)  # unproven: clamped
    step = next(c for c in adjm["chain"] if c["symbol"] == "DEMO")["steps"][0]
    assert step["rule"] == "stance" and step["clamped"] is True and step["conclusion_id"]

    # Ten mature 6-month calls beating both baselines: proven, so AVOID gives x 0.
    for m in range(1, 11):
        cid = add_conclusion(rw_engine, "DEMO", f"2025-{m:02d}-05", "AVOID")
        add_outcome(rw_engine, cid, "6m", hit=1, hold=0, mom=0)
    publish_targets(rw_engine, CONFIG, today=date(2026, 10, 2), weights=W)
    w, _ = published(rw_engine, "2026-10-02")
    assert "DEMO" not in w and w[CORE] == pytest.approx(0.45)

    # Without weights (layer 2 off) or with a stale stance, nothing changes.
    publish_targets(rw_engine, CONFIG, today=date(2026, 10, 3))
    assert published(rw_engine, "2026-10-03")[0]["DEMO"] == pytest.approx(0.15)
    publish_targets(rw_engine, CONFIG, today=date(2026, 11, 15), weights=W)
    assert published(rw_engine, "2026-11-15")[0]["DEMO"] == pytest.approx(0.15)
