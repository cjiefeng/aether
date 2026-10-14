"""M16 pure-play tilt for Aggressive (spec §6.5, §6.6.1, §6.10 check 8, §11). Synthetic data only:
ACME… are pure-plays, ADJA… adjacent names. Medium's buckets (M17) are in test_m17_medium.py."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from starlette.testclient import TestClient

from aether.config import ProfileParams, load_strategies
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import tickers
from aether.portfolio.job import Inputs, compute_run, run_strategies
from aether.portfolio.overlay import Finding, OverlayBuckets, StanceAdj, apply_overlay
from aether.portfolio.select import Candidate, choose
from aether.portfolio.strategies import Buckets, target_weights
from aether.portfolio.thesis import Name, breakdown, split_lines
from aether.portfolio.total_return import align, simple_returns, total_return_levels
from aether.universe.full import illustrative_weights
from tests.conftest import CONFIG_DIR
from tests.m16_golden import ADJACENT, PURE, inputs
from tests.portfolio_data import panel, random_closes, seed_panel, weekdays

CFG = load_strategies(CONFIG_DIR)
AGG = CFG.profiles["aggressive"]
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


def aggressive_weights(out: Any) -> dict[str, dict[str, float]]:
    return {s: w for s, w in out.weights.items() if s.startswith("aggressive:")}


# --------------------------------------------------------------------------- config


def test_config_aggressive_buckets_and_families() -> None:
    assert (AGG.adjacent_max_total, AGG.adjacent_max_per_name, AGG.pure_play_min_total) == (
        0.20,
        0.10,
        0.65,
    )
    assert AGG.families == ("core_equal", "core_momentum")
    assert AGG.max_per_name == 0.35 and AGG.qtum_weight == 0.15
    safe = CFG.profiles["safe"]
    assert not safe.buckets and safe.pure_play_min_total is None and len(safe.families) == 4


@pytest.mark.parametrize(
    "update",
    [
        {"adjacent_max_total": 0.2},  # per-name missing
        {"adjacent_max_per_name": 0.1},  # total missing
        {"adjacent_max_total": 0.1, "adjacent_max_per_name": 0.2},
        {"families": []},
        {"families": ["core_equal", "core_equal"]},
        {"families": ["core_magic"]},
    ],
)
def test_config_rejects_bad_buckets(update: dict[str, Any]) -> None:
    raw = {**CFG.profiles["safe"].model_dump(mode="json"), **update}
    with pytest.raises(ValidationError):
        ProfileParams.model_validate(raw)


# --------------------------------------------------------------------------- allocation


def test_aggressive_buckets_hold_for_every_allowed_family() -> None:
    out = compute_run(typed(), CFG)
    assert out is not None
    ws = aggressive_weights(out)
    assert sorted(ws) == ["aggressive:core_equal:q15", "aggressive:core_momentum:q15"]
    for w in ws.values():
        sp = split(w)
        assert sp["adjacent"] <= 0.20 + EPS
        assert all(w.get(s, 0.0) <= 0.10 + EPS for s in ADJACENT)
        assert all(w.get(s, 0.0) <= 0.35 + EPS for s in PURE)
        assert sp["pure_play"] >= 0.65 - EPS
        assert sp["qtum"] == pytest.approx(0.15)
        assert sum(w.values()) == pytest.approx(1.0)
    sel = out.summary["profiles"]["aggressive"]
    assert sel["strategy_id"] in ws
    assert sel["not_allowed"] == ["core_inv_vol", "core_min_var"]
    assert sel["split"]["pure_play"] == pytest.approx(0.65)
    assert sel["bucket_note"] is None and sel["pure_play_flag"] is False
    # Safe keeps all four families and no buckets (medium: test_m17_medium.py).
    assert out.summary["profiles"]["safe"]["buckets"] is None
    assert out.summary["profiles"]["safe"]["not_allowed"] == []


def test_select_never_picks_a_disallowed_family() -> None:
    good = {"sortino": 1.0, "max_drawdown": 0.5}
    best = {"sortino": 9.0, "max_drawdown": 0.1}
    cands = [
        Candidate("aggressive:core_min_var:q15", best, {"ok": True}, "core_min_var"),
        Candidate("aggressive:core_equal:q15", good, {"ok": True}, "core_equal"),
    ]
    res = choose(cands, AGG)
    assert res["strategy_id"] == "aggressive:core_equal:q15"
    assert res["ranking"] == ["aggressive:core_equal:q15"]


def _R(cols: list[str]) -> np.ndarray:
    closes = typed().closes
    cal = [d for d, _ in closes["QTUM"]]
    return simple_returns(
        np.column_stack([align(total_return_levels(closes[s], {}), cal) for s in cols])
    )


def _bucketed(cols: list[str], family: str = "core_equal") -> dict[str, float]:
    R = _R(cols)
    idx = range(1, len(cols))
    b = Buckets(
        [j for j in idx if TYPES[cols[j]] == "pure_play"],
        [j for j in idx if TYPES[cols[j]] == "adjacent"],
        0.20,
        0.10,
    )
    w = target_weights(
        family,  # type: ignore[arg-type]
        R,
        R.shape[0],
        core=0,
        sleeve=list(idx),
        qtum_weight=AGG.qtum_weight,
        cap=AGG.max_per_name,
        params=CFG.backtest,
        floor=AGG.min_per_name,
        buckets=b,
    )
    return {c: float(v) for c, v in zip(cols, w, strict=True)}


def test_adjacent_overflow_goes_to_pure_plays_not_qtum() -> None:
    # One adjacent name can take only 10% of the 20% bucket; the other 10% goes to the pure-plays.
    w = _bucketed(["QTUM", "ACME", "DEMO", "EXMP", "ADJA"])
    assert w["ADJA"] == pytest.approx(0.10)
    assert w["QTUM"] == pytest.approx(0.15)
    assert w["ACME"] + w["DEMO"] + w["EXMP"] == pytest.approx(0.75)


def test_no_adjacent_names_puts_the_whole_sleeve_in_pure_plays() -> None:
    w = _bucketed(["QTUM", "ACME", "DEMO", "EXMP"], "core_momentum")
    assert w["QTUM"] == pytest.approx(0.15)
    assert max(w[s] for s in ("ACME", "DEMO", "EXMP")) <= 0.35 + EPS


def test_two_pure_plays_still_fill_the_bucket() -> None:
    w = _bucketed(["QTUM", "ACME", "DEMO", "ADJA", "ADJB", "ADJC"])
    assert w["ACME"] == pytest.approx(0.325) and w["DEMO"] == pytest.approx(0.325)
    assert w["ADJA"] + w["ADJB"] + w["ADJC"] == pytest.approx(0.20)
    assert w["QTUM"] == pytest.approx(0.15)


def test_one_pure_play_sends_the_rest_to_qtum_with_a_note_and_flag() -> None:
    out = compute_run(typed(pure=("ACME",)), CFG)
    assert out is not None
    sel = out.summary["profiles"]["aggressive"]
    w = out.weights[sel["strategy_id"]]
    assert w["ACME"] == pytest.approx(0.35)
    assert split(w)["adjacent"] == pytest.approx(0.20)
    assert w["QTUM"] == pytest.approx(0.45)
    assert sel["split"]["qtum"] == pytest.approx(0.45)
    assert "30.0% of the sleeve couldn't be placed and goes to QTUM" in sel["bucket_note"]
    assert sel["pure_play_flag"] is True


def test_full_review_illustrative_weights_follow_the_buckets() -> None:
    w = illustrative_weights([*PURE, *ADJACENT], AGG, set(ADJACENT))
    assert sum(w[s] for s in ADJACENT) == pytest.approx(0.20)
    assert sum(w[s] for s in PURE) == pytest.approx(0.65)
    assert w["QTUM"] == pytest.approx(0.15)
    # Safe (no buckets) is unchanged: equal weight over every name within the cap.
    safe = illustrative_weights([*PURE, *ADJACENT], CFG.profiles["safe"], set(ADJACENT))
    assert len({round(safe[s], 12) for s in (*PURE, *ADJACENT)}) == 1


# --------------------------------------------------------------------------- overlay

SLEEVE = ["ACME", "DEMO", "EXMP", "ADJA", "ADJB"]
BUCKETS = OverlayBuckets(frozenset({"ACME", "DEMO", "EXMP"}), 0.10, 0.20)
BASE = {"QTUM": 0.15, "ACME": 0.25, "DEMO": 0.20, "EXMP": 0.20, "ADJA": 0.10, "ADJB": 0.10}


def cut(sym: str) -> Finding:
    return Finding(sym, "going_concern", 0.0, "0000000000-26-000001", "10-K", "2026-01-01", "", 1)


def test_pure_play_cut_goes_to_other_pure_plays_never_adjacent() -> None:
    pub, _ = apply_overlay(BASE, SLEEVE, 0.35, [cut("ACME")], (), BUCKETS)
    assert "ACME" not in pub
    assert pub["ADJA"] == pytest.approx(0.10) and pub["ADJB"] == pytest.approx(0.10)
    assert pub["DEMO"] == pytest.approx(0.325) and pub["EXMP"] == pytest.approx(0.325)
    assert pub["QTUM"] == pytest.approx(0.15)


def test_pure_play_cuts_beyond_the_caps_go_to_qtum_not_adjacent() -> None:
    pub, _ = apply_overlay(BASE, SLEEVE, 0.35, [cut("ACME"), cut("DEMO")], (), BUCKETS)
    assert pub["EXMP"] == pytest.approx(0.35)
    assert pub["ADJA"] == pytest.approx(0.10) and pub["ADJB"] == pytest.approx(0.10)
    assert pub["QTUM"] == pytest.approx(0.45)
    assert sum(pub.values()) == pytest.approx(1.0)


def test_without_buckets_freed_weight_still_reaches_adjacent_names() -> None:
    base = {**BASE, "ADJA": 0.05, "ADJB": 0.05, "QTUM": 0.25}
    pub, _ = apply_overlay(base, SLEEVE, 0.35, [cut("ACME")])
    assert pub["ADJA"] > 0.05  # the M14 behaviour safe keeps


def test_adjacent_cut_goes_to_the_pure_plays() -> None:
    pub, _ = apply_overlay(BASE, SLEEVE, 0.35, [cut("ADJA")], (), BUCKETS)
    assert "ADJA" not in pub and pub["ADJB"] == pytest.approx(0.10)
    assert pub["ACME"] + pub["DEMO"] + pub["EXMP"] == pytest.approx(0.75)
    assert pub["QTUM"] == pytest.approx(0.15)


def test_accumulate_cannot_lift_the_adjacent_bucket_past_its_total() -> None:
    base = {"QTUM": 0.15, "ACME": 0.20, "DEMO": 0.20, "EXMP": 0.25, "ADJA": 0.0667}
    base |= {"ADJB": 0.0667, "ADJC": 0.0666}
    up = [
        StanceAdj(s, "ACCUMULATE", 1.25, 1.25, False, 7, "2026-01-01", "proven")
        for s in ("ADJA", "ADJB", "ADJC")
    ]
    sleeve = [*SLEEVE, "ADJC"]
    pub, _ = apply_overlay(base, sleeve, 0.35, [], up, BUCKETS)
    assert pub["ADJA"] + pub["ADJB"] + pub["ADJC"] <= 0.20 + EPS
    assert all(pub[s] <= 0.10 + EPS for s in ("ADJA", "ADJB", "ADJC"))
    assert sum(pub.values()) == pytest.approx(1.0)
    assert pub["QTUM"] >= 0.15 - EPS


# --------------------------------------------------------------------------- check 8

NAMES = [
    *(Name(s, "pure_play", "trapped_ion", None) for s in ("ACME", "DEMO", "EXMP")),
    *(Name(s, "adjacent", None, "pqc_cyber") for s in ("ADJA", "ADJB")),
]


def test_pure_play_share_flag_fires_after_an_overlay_cut() -> None:
    cfg = load_strategies(CONFIG_DIR)
    from aether.config import load_thesis_config

    th = load_thesis_config(CONFIG_DIR)
    ok, _ = apply_overlay(BASE, SLEEVE, 0.35, [], (), BUCKETS)
    low, _ = apply_overlay(BASE, SLEEVE, 0.35, [cut("ACME"), cut("DEMO")], (), BUCKETS)
    qtum = {"ACME": 3.0, "DEMO": 2.0, "MSFT": 1.5}  # percent of the fund
    b_ok = breakdown(ok, NAMES, th, qtum, cfg.profiles["aggressive"].pure_play_min_total)
    b_low = breakdown(low, NAMES, th, qtum, 0.65)
    assert b_ok["pure_play_flag"] is False
    assert b_low["pure_play_flag"] is True
    assert b_low["split"]["pure_play"] == pytest.approx(0.35)
    assert b_low["split"]["adjacent"] == pytest.approx(0.20)
    # Look-through: direct 35% + QTUM 45% x 5% of QTUM in the tracked pure-plays.
    assert b_low["split"]["look_through"] == pytest.approx(0.35 + 0.45 * 0.05)
    assert breakdown(low, NAMES, th)["split"]["look_through"] is None  # no snapshot
    assert breakdown(low, NAMES, th)["pure_play_flag"] is None  # no minimum: no check
    lines = split_lines({"targets": {"aggressive": b_low}})
    assert lines[0].startswith("Aggressive targets: QTUM 45% / pure-play 35% / adjacent 20%")
    assert "below the 65% minimum" in lines[1]


# --------------------------------------------------------------------------- pages


@pytest.fixture
def bucketed_run(rw_engine: Engine) -> Engine:
    closes = panel()
    days = weekdays(len(closes["QTUM"]))
    for i, s in enumerate(("ADJA", "ADJB", "ADJC")):
        closes[s] = list(zip(days, random_closes(len(days), seed=500 + i, vol=0.012), strict=True))
    with write_tx(rw_engine) as conn:
        upsert(
            conn,
            tickers,
            [
                {"symbol": s, "type": "adjacent", "sector": "pqc_cyber", "active": 1}
                for s in ("ADJA", "ADJB", "ADJC")
            ],
            key_cols=["symbol"],
        )
    seed_panel(rw_engine, closes)
    run_strategies(rw_engine, CFG)
    return rw_engine


def test_strategies_page_shows_buckets_split_and_not_allowed(
    bucketed_run: Engine, client: TestClient
) -> None:
    text = client.get("/strategies").text
    assert "Buckets: adjacent names ≤ 20.0% in all and ≤ 10.0% each" in text
    assert "(base, before the research overlay): QTUM 15.0% / pure-play 65.0%" in text
    # min-var and inv-vol on aggressive, min-var on medium (M17)
    assert text.count("not allowed for this profile") == 3
