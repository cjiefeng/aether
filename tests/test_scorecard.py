"""M9: scorecard components on fixtures (spec §6.1). Every symbol is synthetic except QTUM, which
the code keys on; every number is made up by the test."""

from __future__ import annotations

import json
import math
from datetime import date

import pytest
from sqlalchemy import Engine, select

from aether.config import load_rubric, load_weights
from aether.db.models import scorecards
from aether.risk.flags import Flag
from aether.score import fundamentals as fnd
from aether.score import scorecard as sc
from aether.score.prices import PriceSeries
from tests.conftest import CONFIG_DIR
from tests.fundamentals_data import CASH, COMMON, add_facts, cash_flow_history, fact
from tests.holdings_data import add_event
from tests.test_reactions import closes_from, market, seed

W = load_weights(CONFIG_DIR)
P = W.scorecard
AS_OF = date(2026, 6, 30)


def test_anchor_map_is_piecewise_linear_and_clamped() -> None:
    a = P.anchors["runway_months"]  # (6,-1) (12,-0.5) (24,0) (48,1)
    assert a.score(0) == -1.0 and a.score(100) == 1.0
    assert a.score(9) == pytest.approx(-0.75)
    assert a.score(36) == pytest.approx(0.5)


def test_config_rejects_unknown_component_and_unordered_anchors() -> None:
    from pydantic import ValidationError

    from aether.config import AnchorMap, WeightsConfig

    raw = W.model_dump(mode="json")
    raw["scorecard"]["weights"]["vibes"] = 1.0
    with pytest.raises(ValidationError):
        WeightsConfig.model_validate(raw)
    with pytest.raises(ValidationError):
        AnchorMap.model_validate({"points": [[2, 0], [1, 1]]})
    with pytest.raises(ValidationError):
        AnchorMap.model_validate({"points": [[0, 0], [1, 2]]})


def test_decay_halves_weight_each_half_life() -> None:
    rows = [
        {"published_at": "2026-06-30T00:00:00Z", "materiality": 4, "direction": 1, "confidence": 1},
        {"published_at": "2026-05-16T00:00:00Z", "materiality": 4, "direction": 1, "confidence": 1},
    ]
    s, n = sc.decayed_sum(rows, AS_OF, 45)
    assert n == 2 and s == pytest.approx(4 + 2)  # second is exactly 45 days old
    c = sc.momentum_component(rows, AS_OF, P)
    assert c["score"] == pytest.approx(math.tanh(6 / P.momentum_scale), abs=1e-6)


def test_risk_load_counts_open_flags() -> None:
    flags = [Flag("ACME", "active_atm", "atm", "2026-01-01")]
    c = sc.risk_component([], flags, AS_OF, P)
    assert c["score"] == pytest.approx(math.tanh(-P.open_flag_penalty / P.risk_scale), abs=1e-6)
    assert c["metrics"]["open_flags"] == ["active_atm"]


def test_fundamentals_component_not_burning_scores_plus_one() -> None:
    snap = fnd.Snapshot("ACME", AS_OF, not_burning=True)
    c = sc.fundamentals_component(snap, P)
    assert c["subscores"]["runway"] == 1.0 and c["score"] == 1.0
    empty = sc.fundamentals_component(fnd.Snapshot("ACME", AS_OF), P)
    assert empty["score"] is None and empty["reason"]


def test_dilution_component_atm_beats_shelf() -> None:
    snap = fnd.Snapshot("ACME", AS_OF)
    atm = [Flag("ACME", "active_atm", "", ""), Flag("ACME", "active_shelf", "", "")]
    assert sc.dilution_component(snap, atm, P)["subscores"]["shelf_atm"] == P.atm_active_score
    shelf = [Flag("ACME", "active_shelf", "", "")]
    assert sc.dilution_component(snap, shelf, P)["subscores"]["shelf_atm"] == P.shelf_active_score
    assert sc.dilution_component(snap, [], P)["subscores"]["shelf_atm"] == 0.0


def test_noise_ratio_hype_flag_and_minimum_events() -> None:
    c = sc.noise_component(["NOISE", "NOISE", "NOISE", "SIGNAL"], 0.2, P)
    assert c["metrics"]["ratio"] == 0.75 and c["metrics"]["hype"] is True
    assert c["score"] == pytest.approx(P.anchors["noise_ratio"].score(0.75))
    assert (
        sc.noise_component(["NOISE", "NOISE", "NOISE", "SIGNAL"], -0.1, P)["metrics"]["hype"]
        is False
    )
    few = sc.noise_component(["NOISE"], 0.2, P)
    assert few["score"] is None


def test_catalyst_component_hit_rate() -> None:
    c = sc.catalyst_component(3, 1, 1, P)
    assert c["subscores"]["hit_rate"] == pytest.approx(0.0)
    assert c["subscores"]["upcoming"] == pytest.approx(0.5)
    assert sc.catalyst_component(0, 0, 0, P)["subscores"]["hit_rate"] is None


def test_price_metrics_drawdown_and_relative_performance() -> None:
    days = ["2026-01-02", "2026-03-31", "2026-06-30"]
    s = PriceSeries("ACME", tuple(days), (10.0, 20.0, 15.0), (1.0, 2.0, 1.5), (1, 1, 1))
    q = PriceSeries("QTUM", tuple(days), (10.0, 10.0, 11.0), (1.0, 1.0, 1.1), (1, 1, 1))
    m = sc.price_metrics(s, q, AS_OF, 0.8)
    assert m["drawdown_52w"] == pytest.approx(-0.25)
    assert m["return_90d"] == pytest.approx(-0.25)  # level on/before 2026-04-01 is 2.0
    assert m["rel_perf_90d"] == pytest.approx(-0.25 - 0.1)
    assert m["iv_30"] == 0.8


def test_total_skips_missing_and_reports_coverage() -> None:
    comps = {
        "fundamentals": {"score": 0.5},
        "dilution": {"score": None},
        "market_reaction": {"score": 1.0},  # weight 0: never counts
    }
    w = {"fundamentals": 1.0, "dilution": 1.0, "market_reaction": 0.0}
    tot, cov = sc.total(comps, w)
    assert tot == pytest.approx(50.0) and cov == pytest.approx(0.5)
    assert sc.total({"dilution": {"score": None}}, w) == (None, 0.0)


def test_job_writes_scorecards_for_pure_plays_and_qtum(rw_engine: Engine) -> None:
    days, rb, rs = market(300, AS_OF)
    seed(
        rw_engine,
        days,
        {"QTUM": closes_from(rb), "ACME": closes_from(rs), "DEMO": closes_from(rs[::-1])},
    )
    add_facts(
        rw_engine,
        [
            *cash_flow_history("ACME", -25),
            fact("ACME", CASH, "2026-03-31", 300, filed="2026-05-01"),
            fact("ACME", COMMON, "2026-04-30", 1000, filed="2026-05-01"),
        ],
    )
    add_event(rw_engine, "ACME", "2026-06-20T14:00:00Z", 4, "SIGNAL", "contract_with_value")
    rubric = load_rubric(CONFIG_DIR)
    assert sc.run_scorecards(rw_engine, W, rubric).rows_written == 5  # 4 pure-plays + QTUM
    assert sc.run_scorecards(rw_engine, W, rubric).rows_written == 0  # same inputs: no rewrite
    with rw_engine.connect() as conn:
        rows = {r.symbol: r for r in conn.execute(select(scorecards))}
    acme = json.loads(rows["ACME"].components)
    assert rows["ACME"].as_of == days[-1]
    mom = acme["components"]["signal_momentum"]
    assert mom["metrics"]["events"] == 1 and mom["score"] < 0  # add_event sets direction -1
    fund = acme["components"]["fundamentals"]["metrics"]["snapshot"]
    assert fund["runway_months"] is not None
    assert -100 <= rows["ACME"].total <= 100 and 0 < rows["ACME"].coverage <= 1
    qtum = json.loads(rows["QTUM"].components)["components"]
    assert "fundamentals" not in qtum and "price_context" in qtum
    # FAKE/EXMP have no prices or facts: still a row, with the gaps named.
    assert "fundamentals" in json.loads(rows["FAKE"].components)["missing"]


def test_scheduler_registers_m9_jobs(rw_engine: Engine, migrated_db) -> None:  # type: ignore[no-untyped-def]
    from aether.jobs import build_scheduler
    from tests.conftest import make_settings

    sched = build_scheduler(rw_engine, make_settings(migrated_db))
    want = {"reactions": ("6", "45"), "scores": ("7", "0"), "calibration": ("8", "0")}
    for jid, (hour, minute) in want.items():
        job_ = sched.get_job(jid)
        assert job_ is not None, jid
        fields = {f.name: str(f) for f in job_.trigger.fields}
        assert (fields["hour"], fields["minute"]) == (hour, minute)
    cal = {f.name: str(f) for f in sched.get_job("calibration").trigger.fields}
    assert cal["day_of_week"] == "sun"
