"""M17 Medium in the middle (spec §6.5, §6.6.1, §6.10 check 8, §11). Medium uses the M16 bucket
mechanism with middle values: adjacent <= 20% (10% each), pure-plays >= 35% (20% each), QTUM 45%,
no min-variance. Synthetic data only: ACME… are pure-plays, ADJA… adjacent names."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import numpy as np
import pytest
from sqlalchemy import Engine, select

from aether.config import load_strategies, load_thesis_config, load_weights
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import profile_targets, tickers
from aether.portfolio.job import Inputs, compute_run
from aether.portfolio.overlay import Finding, OverlayBuckets, apply_overlay
from aether.portfolio.publish import publish_targets
from aether.portfolio.select import Candidate, choose
from aether.portfolio.strategies import Buckets, target_weights
from aether.portfolio.thesis import Name, breakdown, split_lines
from aether.portfolio.total_return import align, simple_returns, total_return_levels
from aether.universe.full import illustrative_weights
from tests.conclusions_data import add_conclusion, add_outcome
from tests.conftest import CONFIG_DIR
from tests.holdings_data import fake_run, seed_prices
from tests.m16_golden import ADJACENT, PURE, inputs

CFG = load_strategies(CONFIG_DIR)
MED = CFG.profiles["medium"]
TYPES = {**{s: "pure_play" for s in PURE}, **{s: "adjacent" for s in ADJACENT}}
EPS = 1e-9


def typed(pure: tuple[str, ...] = PURE, adjacent: tuple[str, ...] = ADJACENT) -> Inputs:
    base = inputs()
    keep = (*pure, *adjacent)
    closes = {s: v for s, v in base.closes.items() if s not in TYPES or s in keep}
    return Inputs(tuple(sorted(keep)), closes, {}, {s: TYPES[s] for s in keep})


def split(w: dict[str, float]) -> dict[str, float]:
    out = {"qtum": 0.0, "pure_play": 0.0, "adjacent": 0.0}
    for s, v in w.items():
        out["qtum" if s == "QTUM" else TYPES[s]] += v
    return out


def medium_weights(out: Any) -> dict[str, dict[str, float]]:
    return {s: w for s, w in out.weights.items() if s.startswith("medium:")}


# --------------------------------------------------------------------------- config


def test_config_medium_buckets_and_families() -> None:
    assert (MED.adjacent_max_total, MED.adjacent_max_per_name, MED.pure_play_min_total) == (
        0.20,
        0.10,
        0.35,
    )
    assert MED.families == ("core_equal", "core_momentum", "core_inv_vol")
    assert (MED.qtum_weight, MED.max_per_name, MED.min_per_name) == (0.45, 0.20, 0.03)
    assert MED.rank_metric == "sortino_high"


def test_the_ladder_steps_down_one_family_at_a_time() -> None:
    fams = [set(CFG.profiles[p].families) for p in ("safe", "medium", "aggressive")]  # type: ignore[index]
    assert [len(f) for f in fams] == [4, 3, 2]
    assert fams[0] > fams[1] > fams[2]
    # Same supplier budget in Medium and Aggressive: they differ in QTUM and pure-play share.
    agg = CFG.profiles["aggressive"]
    assert (MED.adjacent_max_total, MED.adjacent_max_per_name) == (
        agg.adjacent_max_total,
        agg.adjacent_max_per_name,
    )


# --------------------------------------------------------------------------- allocation


def test_medium_buckets_hold_for_every_allowed_family() -> None:
    out = compute_run(typed(), CFG)
    assert out is not None
    ws = medium_weights(out)
    assert sorted(ws) == [
        "medium:core_equal:q45",
        "medium:core_inv_vol:q45",
        "medium:core_momentum:q45",
    ]
    for w in ws.values():
        sp = split(w)
        assert sp["adjacent"] <= 0.20 + EPS
        assert all(w.get(s, 0.0) <= 0.10 + EPS for s in ADJACENT)
        assert all(w.get(s, 0.0) <= 0.20 + EPS for s in PURE)
        assert sp["pure_play"] >= 0.35 - EPS
        assert sp["qtum"] == pytest.approx(0.45)
        assert sum(w.values()) == pytest.approx(1.0)
    sel = out.summary["profiles"]["medium"]
    assert sel["strategy_id"] in ws
    assert sel["not_allowed"] == ["core_min_var"]
    assert sel["split"]["qtum"] == pytest.approx(0.45)
    assert sel["split"]["pure_play"] == pytest.approx(0.35)
    assert sel["split"]["adjacent"] == pytest.approx(0.20)
    assert sel["buckets"]["pure_play_min_total"] == 0.35
    assert sel["bucket_note"] is None and sel["pure_play_flag"] is False


def test_select_never_picks_min_var_for_medium() -> None:
    good = {"sortino": 1.0, "max_drawdown": 0.5}
    best = {"sortino": 9.0, "max_drawdown": 0.1}
    cands = [
        Candidate("medium:core_min_var:q45", best, {"ok": True}, "core_min_var"),
        Candidate("medium:core_inv_vol:q45", good, {"ok": True}, "core_inv_vol"),
    ]
    res = choose(cands, MED)
    assert res["strategy_id"] == "medium:core_inv_vol:q45"  # inverse-vol stays allowed
    assert res["ranking"] == ["medium:core_inv_vol:q45"]


def _bucketed(cols: list[str], family: str = "core_equal") -> dict[str, float]:
    closes = typed().closes
    cal = [d for d, _ in closes["QTUM"]]
    R = simple_returns(
        np.column_stack([align(total_return_levels(closes[s], {}), cal) for s in cols])
    )
    idx = range(1, len(cols))
    b = Buckets(
        [j for j in idx if TYPES[cols[j]] == "pure_play"],
        [j for j in idx if TYPES[cols[j]] == "adjacent"],
        MED.adjacent_max_total,  # type: ignore[arg-type]
        MED.adjacent_max_per_name,  # type: ignore[arg-type]
    )
    w = target_weights(
        family,  # type: ignore[arg-type]
        R,
        R.shape[0],
        core=0,
        sleeve=list(idx),
        qtum_weight=MED.qtum_weight,
        cap=MED.max_per_name,
        params=CFG.backtest,
        floor=MED.min_per_name,
        buckets=b,
    )
    return {c: float(v) for c, v in zip(cols, w, strict=True)}


@pytest.mark.parametrize("family", ["core_equal", "core_inv_vol", "core_momentum"])
def test_adjacent_overflow_goes_to_pure_plays_not_qtum(family: str) -> None:
    # One adjacent name can take only 10% of the 20% bucket; the other 10% goes to the pure-plays.
    w = _bucketed(["QTUM", "ACME", "DEMO", "EXMP", "ADJA"], family)
    assert w["ADJA"] == pytest.approx(0.10)
    assert w["QTUM"] == pytest.approx(0.45)
    assert w["ACME"] + w["DEMO"] + w["EXMP"] == pytest.approx(0.45)
    assert max(w[s] for s in ("ACME", "DEMO", "EXMP")) <= 0.20 + EPS


def test_two_pure_plays_fill_the_bucket_with_nothing_to_qtum() -> None:
    w = _bucketed(["QTUM", "ACME", "DEMO", "ADJA", "ADJB", "ADJC"])
    assert w["ACME"] == pytest.approx(0.175) and w["DEMO"] == pytest.approx(0.175)
    assert w["ADJA"] + w["ADJB"] + w["ADJC"] == pytest.approx(0.20)
    assert w["QTUM"] == pytest.approx(0.45)
    out = compute_run(typed(pure=("ACME", "DEMO")), CFG)
    assert out is not None
    sel = out.summary["profiles"]["medium"]
    assert sel["bucket_note"] is None and sel["pure_play_flag"] is False
    assert sel["split"]["qtum"] == pytest.approx(0.45)


def test_one_pure_play_sends_the_rest_to_qtum_with_a_note_and_flag() -> None:
    out = compute_run(typed(pure=("ACME",)), CFG)
    assert out is not None
    sel = out.summary["profiles"]["medium"]
    w = out.weights[sel["strategy_id"]]
    assert w["ACME"] == pytest.approx(0.20)
    assert split(w)["adjacent"] == pytest.approx(0.20)
    assert w["QTUM"] == pytest.approx(0.60)
    assert sel["split"]["qtum"] == pytest.approx(0.60)
    assert "15.0% of the sleeve couldn't be placed and goes to QTUM" in sel["bucket_note"]
    assert sel["pure_play_flag"] is True


def test_full_review_illustrative_weights_follow_medium_buckets() -> None:
    w = illustrative_weights([*PURE, *ADJACENT], MED, set(ADJACENT))
    assert sum(w[s] for s in ADJACENT) == pytest.approx(0.20)
    assert sum(w[s] for s in PURE) == pytest.approx(0.35)
    assert w["QTUM"] == pytest.approx(0.45)


# --------------------------------------------------------------------------- overlay

SLEEVE = ["ACME", "DEMO", "EXMP", "ADJA", "ADJB"]
BUCKETS = OverlayBuckets(
    frozenset({"ACME", "DEMO", "EXMP"}),
    MED.adjacent_max_per_name,  # type: ignore[arg-type]
    MED.adjacent_max_total,  # type: ignore[arg-type]
)
BASE = {"QTUM": 0.45, "ACME": 0.12, "DEMO": 0.12, "EXMP": 0.11, "ADJA": 0.10, "ADJB": 0.10}


def cut(sym: str) -> Finding:
    return Finding(sym, "going_concern", 0.0, "0000000000-26-000001", "10-K", "2026-01-01", "", 1)


def test_pure_play_cut_goes_to_other_pure_plays_never_adjacent() -> None:
    pub, _ = apply_overlay(BASE, SLEEVE, MED.max_per_name, [cut("ACME")], (), BUCKETS)
    assert "ACME" not in pub
    assert pub["ADJA"] == pytest.approx(0.10) and pub["ADJB"] == pytest.approx(0.10)
    assert pub["DEMO"] + pub["EXMP"] == pytest.approx(0.35)
    assert max(pub["DEMO"], pub["EXMP"]) <= 0.20 + EPS
    assert pub["QTUM"] == pytest.approx(0.45)


def test_pure_play_cuts_beyond_the_caps_go_to_qtum_not_adjacent() -> None:
    pub, _ = apply_overlay(BASE, SLEEVE, MED.max_per_name, [cut("ACME"), cut("DEMO")], (), BUCKETS)
    assert pub["EXMP"] == pytest.approx(0.20)
    assert pub["ADJA"] == pytest.approx(0.10) and pub["ADJB"] == pytest.approx(0.10)
    assert pub["QTUM"] == pytest.approx(0.60)
    assert sum(pub.values()) == pytest.approx(1.0)


NAMES = [
    *(Name(s, "pure_play", "trapped_ion", None) for s in ("ACME", "DEMO", "EXMP")),
    *(Name(s, "adjacent", None, "pqc_cyber") for s in ("ADJA", "ADJB")),
]


def test_pure_play_share_flag_fires_when_a_cut_takes_medium_below_35() -> None:
    th = load_thesis_config(CONFIG_DIR)
    ok, _ = apply_overlay(BASE, SLEEVE, MED.max_per_name, [], (), BUCKETS)
    low, _ = apply_overlay(BASE, SLEEVE, MED.max_per_name, [cut("ACME"), cut("DEMO")], (), BUCKETS)
    b_ok = breakdown(ok, NAMES, th, None, MED.pure_play_min_total)
    b_low = breakdown(low, NAMES, th, None, MED.pure_play_min_total)
    assert b_ok["pure_play_flag"] is False
    assert b_low["pure_play_flag"] is True
    assert b_low["split"]["pure_play"] == pytest.approx(0.20)
    lines = split_lines({"targets": {"medium": b_low}})
    assert lines[0].startswith("Medium targets: QTUM 60% / pure-play 20% / adjacent 20%")
    assert "below the 35% minimum" in lines[1]


def test_publish_routes_medium_freed_weight_past_adjacent_names(rw_engine: Engine) -> None:
    seed_prices(rw_engine, n=200, start=date(2026, 1, 2))
    with write_tx(rw_engine) as conn:  # FAKE plays the adjacent name here
        upsert(
            conn,
            tickers,
            [{"symbol": "FAKE", "type": "adjacent", "sector": "pqc_cyber", "active": 1}],
            key_cols=["symbol"],
        )
    base = {"QTUM": 0.45, "ACME": 0.20, "DEMO": 0.15, "EXMP": 0.10, "FAKE": 0.10}
    fake_run(rw_engine, "2026-09-30", {"medium": base})
    # A proven AVOID (ten mature calls beating both baselines) zeroes DEMO.
    add_conclusion(rw_engine, "DEMO", "2026-09-27", "AVOID")
    for m in range(1, 11):
        cid = add_conclusion(rw_engine, "DEMO", f"2025-{m:02d}-05", "AVOID")
        add_outcome(rw_engine, cid, "6m", hit=1, hold=0, mom=0)
    publish_targets(rw_engine, CFG, today=date(2026, 10, 1), weights=load_weights(CONFIG_DIR))
    with rw_engine.connect() as conn:
        w = json.loads(
            conn.execute(
                select(profile_targets.c.published_weights).where(
                    profile_targets.c.profile == "medium"
                )
            ).scalar_one()
        )
    # DEMO's 15%: EXMP fills to its 20% cap, ACME is already there, the rest goes to QTUM.
    assert "DEMO" not in w
    assert w["FAKE"] == pytest.approx(0.10)
    assert w["EXMP"] == pytest.approx(0.20) and w["ACME"] == pytest.approx(0.20)
    assert w["QTUM"] == pytest.approx(0.50)
    assert sum(w.values()) == pytest.approx(1.0)
