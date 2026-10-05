"""Options analytics (M8, spec §6.8): skew and the straddle-implied move checked by hand against
named contracts of the real IONQ chain recorded 2026-10-04; the implied move picks the first
listed expiry after a synthetic catalyst; thin chains are flagged, never reported; IV rank and
volume history; and no options metric reaches published targets."""

from __future__ import annotations

import gzip
import json
import math
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select

from aether.config import load_options_config, load_strategies
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import catalysts, options_snapshots, profile_targets
from aether.options.analytics import CatalystDate, history_metrics, implied_moves, skew_30
from aether.options.job import snapshot_options
from aether.options.snapshot import choose_expiries, compute
from aether.portfolio.publish import publish_targets
from aether.providers.options import YFinanceOptions, parse_chain
from tests.conftest import CONFIG_DIR, REPO
from tests.holdings_data import fake_run, seed_prices, seed_universe

FIX = Path(__file__).parent / "fixtures" / "options"
IONQ: dict[str, Any] = json.loads(gzip.decompress((FIX / "IONQ_2026-10-04.json.gz").read_bytes()))
OPT = load_options_config(CONFIG_DIR)
D0 = date(2026, 10, 4)
N = NormalDist()


def contract(expiry: str, side: str, strike: float) -> dict[str, Any]:
    e = next(x for x in IONQ["expiries"] if x["expiry"] == expiry)
    return next(c for c in e[side] if c["strike"] == strike)


def hand_delta(call: bool, strike: float, iv: float, dte: int) -> float:
    s, t = IONQ["spot"], dte / 365
    d1 = (math.log(s / strike) + iv * iv * t / 2) / (iv * math.sqrt(t))
    return N.cdf(d1) - (0 if call else 1)


def hand_iv_at(expiry: str, side: str, lo: float, hi: float, dte: int, target: float) -> float:
    """Linear in delta between two named strikes."""
    a, b = contract(expiry, side, lo), contract(expiry, side, hi)
    call = side == "calls"
    da = hand_delta(call, lo, a["impliedVolatility"], dte)
    db = hand_delta(call, hi, b["impliedVolatility"], dte)
    assert min(da, db) <= target <= max(da, db)  # the named strikes bracket the target
    return a["impliedVolatility"] + (b["impliedVolatility"] - a["impliedVolatility"]) * (
        target - da
    ) / (db - da)


def test_skew_matches_hand_computation_on_recorded_chain() -> None:
    """Acceptance: options metrics match hand-computed values on a recorded chain fixture."""
    # 30 days from 2026-10-04 lies between the 2026-10-30 (26 d) and 2026-11-06 (33 d) expiries.
    s26 = hand_iv_at("2026-10-30", "puts", 40, 39, 26, -0.25) - hand_iv_at(
        "2026-10-30", "calls", 50, 55, 26, 0.25
    )
    s33 = hand_iv_at("2026-11-06", "puts", 40, 38, 33, -0.25) - hand_iv_at(
        "2026-11-06", "calls", 50, 55, 33, 0.25
    )
    expected = s26 + (s33 - s26) * (30 - 26) / (33 - 26)
    got, why = skew_30(parse_chain(IONQ), D0, OPT)
    assert why is None
    assert got["value"] == pytest.approx(expected, abs=2e-6)
    rows = {r["expiry"]: r for r in got["expiries"]}
    assert rows["2026-10-30"]["skew"] == pytest.approx(s26, abs=2e-6)
    assert rows["2026-11-06"]["skew"] == pytest.approx(s33, abs=2e-6)


def test_implied_move_matches_straddle_by_hand_and_uses_first_expiry_after() -> None:
    """Acceptance: the implied move uses the first expiry after a synthetic catalyst."""
    chain = parse_chain(IONQ)
    cats = [
        CatalystDate(1, "ACME synthetic catalyst", date(2026, 10, 31)),  # → 2026-11-06
        CatalystDate(2, "on an expiry day", date(2026, 10, 30)),  # strictly after → 2026-11-06
        CatalystDate(3, "before the first expiry", date(2026, 10, 5)),  # → 2026-10-09
    ]
    moves = {m["catalyst_id"]: m for m in implied_moves(chain, D0, cats, OPT)}
    assert moves[1]["expiry"] == "2026-11-06" and moves[2]["expiry"] == "2026-11-06"
    assert moves[3]["expiry"] == "2026-10-09"
    # By hand: the strike nearest the 43.77 spot listed as both call and put on 2026-11-06 is 44.
    c, p = contract("2026-11-06", "calls", 44.0), contract("2026-11-06", "puts", 44.0)
    straddle = (c["bid"] + c["ask"]) / 2 + (p["bid"] + p["ask"]) / 2
    assert moves[1]["strike"] == 44.0
    assert moves[1]["straddle"] == pytest.approx(straddle, abs=1e-6)
    assert moves[1]["move"] == pytest.approx(straddle / IONQ["spot"], abs=1e-6)


def test_implied_move_needs_the_true_first_listed_expiry() -> None:
    raw = {**IONQ, "listed": [*[e["expiry"] for e in IONQ["expiries"]], "2026-11-01"]}
    m = implied_moves(parse_chain(raw), D0, [CatalystDate(1, "x", date(2026, 10, 31))], OPT)[0]
    assert m["move"] is None and "2026-11-01" in m["reason"]
    late = implied_moves(parse_chain(IONQ), D0, [CatalystDate(2, "y", date(2027, 6, 1))], OPT)[0]
    assert late["move"] is None and "not extrapolated" in late["reason"]


def test_choose_expiries_adds_first_expiry_after_each_catalyst() -> None:
    listed = [
        date(2026, 10, 9),
        date(2026, 10, 16),
        date(2026, 10, 23),
        date(2026, 10, 30),
        date(2026, 11, 6),
        date(2026, 11, 20),
        date(2026, 12, 18),
        date(2027, 1, 15),
    ]
    base = choose_expiries(listed, D0, OPT)
    with_cat = choose_expiries(listed, D0, OPT, [date(2026, 10, 20)])
    assert date(2026, 10, 23) not in base and date(2026, 10, 23) in with_cat


def _thin(raw: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(raw))
    for e in out["expiries"]:
        for side in ("calls", "puts"):
            for c in e[side]:
                c["openInterest"] = 0
    return out


def test_thin_chain_is_flagged_not_reported() -> None:
    """Acceptance: a thin chain is flagged, not reported."""
    cats = [CatalystDate(1, "x", date(2026, 10, 31))]
    m, q = compute(parse_chain(_thin(IONQ)), D0, OPT, cats)
    assert q["thin"] is True
    assert m["atm_iv_30"] is None and m["skew_30"] is None
    assert all(v is None for v in m["term"].values())
    assert [x["move"] for x in m["implied_moves"]] == [None]
    assert q["reasons"]["skew_30"] == "thin chain"


# --------------------------------------------------------------------------- history


def test_iv_rank_builds_history_then_ranks() -> None:
    h, why = history_metrics(0.8, 100, [(0.5, 90)] * 10)
    assert h["iv_rank"] is None and "building history (11 days of 252)" in why["iv_rank"]
    assert "building history" in why["volume_vs_median"]
    past = [(0.4 + 0.4 * i / 250, 100 + i) for i in range(251)]  # 0.40 .. 0.80
    h, why = history_metrics(0.6, 400, past)
    assert h["iv_rank"] == pytest.approx(0.5, abs=1e-6)
    assert h["iv_percentile"] == pytest.approx(125 / 251, abs=1e-6)
    med = sorted(v for _, v in past[-20:])[9:11]
    assert h["volume_vs_median"] == pytest.approx(400 / (sum(med) / 2), abs=1e-6)
    assert why == {}


def test_snapshot_job_stores_moves_into_db_catalysts_and_history(rw_engine: Engine) -> None:
    seed_universe(rw_engine)
    with write_tx(rw_engine) as conn:
        conn.execute(
            insert(catalysts).values(
                key="earnings:QTUM:2026-10-31",
                origin="earnings",
                symbol="QTUM",
                title="QTUM synthetic catalyst",
                kind="earnings",
                window_start="2026-10-31",
                window_end="2026-10-31",
                updated_at="2026-10-04T00:00:00Z",
            )
        )
        upsert(
            conn,
            options_snapshots,
            [
                {
                    "symbol": "QTUM",
                    "d": "2026-10-03",
                    "metrics": json.dumps({"atm_iv_30": 0.7, "total_volume": 10}),
                    "quality": "{}",
                    "provider": "synthetic",
                    "fetched_at": "2026-10-03T00:00:00Z",
                }
            ],
            key_cols=["symbol", "d"],
        )
    chosen: list[list[date]] = []

    def fetch_raw(symbol: str, choose: Any) -> dict[str, Any]:
        if symbol != "QTUM":
            raise RuntimeError("no chain")
        chosen.append(choose([date.fromisoformat(e["expiry"]) for e in IONQ["expiries"]]))
        return {**IONQ, "symbol": symbol}

    snapshot_options(rw_engine, YFinanceOptions(fetch_raw_fn=fetch_raw), OPT, d=D0)
    assert date(2026, 11, 6) in chosen[0]
    with rw_engine.connect() as conn:
        row = conn.execute(
            select(options_snapshots).where(
                options_snapshots.c.symbol == "QTUM", options_snapshots.c.d == D0.isoformat()
            )
        ).one()
    m, q = json.loads(row.metrics), json.loads(row.quality)
    assert m["implied_moves"][0]["expiry"] == "2026-11-06" and m["implied_moves"][0]["move"] > 0
    assert m["iv_history_days"] == 2 and "building history" in q["reasons"]["iv_rank"]
    assert m["skew_30"] is not None


# --------------------------------------------------------------------------- never in sizing


def test_no_options_metric_reaches_profile_targets(rw_engine: Engine) -> None:
    """Acceptance: wild options snapshots leave published weights and input hashes unchanged."""
    cfg = load_strategies(CONFIG_DIR)
    days = seed_prices(rw_engine)
    fake_run(rw_engine, days[-1], {"safe": {"QTUM": 0.75, "ACME": 0.25}})

    def publish() -> list[tuple[str, str, bytes]]:
        publish_targets(rw_engine, cfg, today=date(2026, 10, 1))
        with rw_engine.connect() as conn:
            return [
                (r.profile, r.published_weights, r.input_hash)
                for r in conn.execute(select(profile_targets).order_by(profile_targets.c.profile))
            ]

    before = publish()
    with write_tx(rw_engine) as conn:
        upsert(
            conn,
            options_snapshots,
            [
                {
                    "symbol": s,
                    "d": "2026-09-30",
                    "metrics": json.dumps({"atm_iv_30": 9.9, "skew_30": 5.0, "iv_rank": 1.0}),
                    "quality": json.dumps({"thin": False}),
                    "provider": "synthetic",
                    "fetched_at": "2026-09-30T00:00:00Z",
                }
                for s in ("QTUM", "ACME", "DEMO", "EXMP", "FAKE")
            ],
            key_cols=["symbol", "d"],
        )
    assert publish() == before
    # And statically: nothing under portfolio/ reads options data.
    for p in (REPO / "src" / "aether" / "portfolio").glob("*.py"):
        text = p.read_text("utf-8")
        assert "options_snapshots" not in text and "aether.options" not in text, p
