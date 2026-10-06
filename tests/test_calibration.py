"""M9: calibration report (spec §6.3) and implied vs realized move on the Calibration page (§6.8).
Synthetic rows and symbols only."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest
from sqlalchemy import Engine, insert, select

from aether.config import load_weights
from aether.db.engine import write_tx
from aether.db.models import calibration_reports, catalysts, options_snapshots
from aether.db.types import utcnow_iso
from aether.score.calibration import build_report, run_calibration
from aether.score.reaction import run_reactions
from tests.conftest import CONFIG_DIR
from tests.holdings_data import add_event
from tests.test_reactions import closes_from, market, seed

W = load_weights(CONFIG_DIR)
P = W.calibration


def row(i: int, cls: str, cat: str, z5: float, **kw: object) -> dict:
    base = {
        "event_id": i,
        "symbol": "ACME",
        "title": f"synthetic {i}",
        "class": cls,
        "category": cat,
        "materiality": 2,
        "direction": 1,
        "status": "complete",
        "approx_time": 0,
        "t0": "2026-05-01",
        "z_1": z5,
        "z_5": z5,
        "car_5": z5 / 100,
        "reversal_ratio": 1.0,
        "abn_volume": 2.0,
    }
    return {**base, **kw}


def test_report_flags_and_minimum_n() -> None:
    rows = [row(i, "NOISE", "analyst_rating", 3.0) for i in range(15)]  # moves like signal
    rows += [row(100 + i, "SIGNAL", "contract_with_value", 0.2) for i in range(15)]  # ignored
    rows += [row(200 + i, "SIGNAL", "m_and_a", 3.0) for i in range(14)]  # one short of n=15
    rows += [
        row(300 + i, "NOISE", "listicle_or_momentum", 3.0, status="confounded") for i in range(20)
    ]
    rows += [row(400 + i, "NOISE", "synthetic_benchmark", 3.0, approx_time=1) for i in range(20)]
    rep = build_report(rows, P)
    kinds = {(f["kind"], f["category"]) for f in rep["flags"]}
    assert kinds == {
        ("noise_like_signal", "analyst_rating"),
        ("signal_ignored", "contract_with_value"),
    }
    cats = {c["category"]: c for c in rep["categories"]}
    assert cats["m_and_a"]["too_few"] and cats["m_and_a"]["n"] == 14
    assert "listicle_or_momentum" not in cats  # confounded rows never count
    assert "synthetic_benchmark" not in cats  # approximate timestamps never count
    assert rep["counts"]["approx_time_excluded"] == 20 and rep["counts"]["confounded"] == 20
    assert rep["classes"]["NOISE"]["n"] == 15
    assert rep["classes"]["SIGNAL"]["direction_hit_rate"] == 1.0


def test_direction_hit_rate_and_spot_review() -> None:
    rows = [row(i, "RISK", "dilution", -1.0, direction=-1, car_5=-0.02) for i in range(10)]
    rows += [row(10 + i, "RISK", "dilution", 1.0, direction=-1, car_5=0.02) for i in range(5)]
    rows += [row(20, "RISK", "dilution", 0.1, direction=0, materiality=5, car_5=0.0)]
    rep = build_report(rows, P)
    risk = rep["classes"]["RISK"]
    assert risk["direction_n"] == 15 and risk["direction_hit_rate"] == pytest.approx(10 / 15)
    assert [s["event_id"] for s in rep["spot_review"]] == [20]


def test_implied_vs_realized_on_a_resolved_catalyst(rw_engine: Engine) -> None:
    days, rb, rs = market(200, date(2026, 6, 30))
    rs = rs.copy()
    rs[150] += 0.08
    seed(rw_engine, days, {"QTUM": closes_from(rb), "ACME": closes_from(rs)})
    t0 = days[150]
    eid = add_event(rw_engine, "ACME", f"{t0}T21:30:00Z", 4, "SIGNAL", "earnings_release")
    # The 8-K lands after the close: t0 is the next session.
    t0 = days[151]
    with write_tx(rw_engine) as conn:
        cid = conn.execute(
            insert(catalysts)
            .values(
                key="earnings:ACME:synthetic",
                origin="earnings",
                symbol="ACME",
                title="ACME earnings (synthetic)",
                kind="earnings",
                window_start=days[150],
                window_end=days[150],
                status="hit",
                resolution="event",
                resolved_by_event_id=eid,
                resolved_at=utcnow_iso(),
                updated_at=utcnow_iso(),
            )
            .returning(catalysts.c.id)
        ).scalar_one()
        for d, move in ((days[148], 0.05), (days[149], 0.07)):
            conn.execute(
                insert(options_snapshots).values(
                    symbol="ACME",
                    d=d,
                    metrics=json.dumps(
                        {"implied_moves": [{"catalyst_id": cid, "move": move, "expiry": days[155]}]}
                    ),
                    quality="{}",
                    provider="synthetic",
                    fetched_at=utcnow_iso(),
                )
            )
    run_reactions(rw_engine, W.reactions)
    run_calibration(rw_engine, P, date(2026, 7, 5))
    with rw_engine.connect() as conn:
        rep = json.loads(conn.execute(select(calibration_reports.c.payload)).scalar_one())
    (iv,) = rep["implied_vs_realized"]
    assert iv["catalyst_id"] == cid and iv["t0"] == t0
    assert iv["implied_move"] == 0.07 and iv["snapshot"] == days[149]  # last snapshot before t0
    assert iv["realized_abs_return_1"] == pytest.approx(abs(np.prod(1 + rs[151:153]) - 1), rel=1e-4)
    assert iv["ratio"] == pytest.approx(iv["realized_abs_return_1"] / 0.07, rel=1e-4)
