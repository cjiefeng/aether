"""M10 acceptance: the conclusion track record and the overlay's value-added rows (spec §6.4,
§6.6.1 layer 3) fill in as synthetic prices mature. Synthetic data only."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import Engine, select

from aether.config import load_strategies, load_weights
from aether.db.models import conclusion_outcomes, overlay_outcomes, prices_daily, profile_targets
from aether.portfolio.publish import publish_targets
from aether.score.track_record import add_months, is_hit, run_track_record, standing
from tests.conclusions_data import add_conclusion, add_outcome, seed_closes, weekdays_between
from tests.conftest import CONFIG_DIR, seed_tickers
from tests.holdings_data import fake_run, seed_prices

W = load_weights(CONFIG_DIR)
P = W.track_record
GROWTH = {"QTUM": 0.001, "QQQ": 0.0005, "ACME": 0.002, "DEMO": -0.001}


def closes(days: list[str], g: float) -> list[tuple[str, float]]:
    return [(d, round(100 * (1 + g) ** i, 6)) for i, d in enumerate(days)]


def seed(engine: Engine, end: date) -> list[str]:
    days = weekdays_between(date(2024, 10, 1), end)
    seed_closes(engine, {s: closes(days, g) for s, g in GROWTH.items()})
    return days


def last_on_or_before(days: list[str], day: str) -> int:
    return max(i for i, d in enumerate(days) if d <= day)


def outcomes(engine: Engine) -> dict[tuple[int, str], dict]:
    with engine.connect() as conn:
        return {
            (r.conclusion_id, r.horizon): dict(r._mapping)
            for r in conn.execute(select(conclusion_outcomes))
        }


@pytest.fixture
def engine(rw_engine: Engine) -> Engine:
    seed_tickers(
        rw_engine,
        [("QTUM", "etf"), ("QQQ", "benchmark"), ("ACME", "pure_play"), ("DEMO", "pure_play")],
    )
    return rw_engine


def test_add_months_and_hit_rules() -> None:
    assert add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert add_months(date(2025, 11, 15), 3) == date(2026, 2, 15)
    assert is_hit("ACCUMULATE", 0.01, 0.1) and not is_hit("ACCUMULATE", -0.01, 0.1)
    assert is_hit("AVOID", -0.01, 0.1) and is_hit("TRIM", -0.2, 0.1)
    assert is_hit("HOLD", 0.05, 0.1) and not is_hit("HOLD", 0.15, 0.1)
    assert is_hit("PURE_PLAYS", 0.02, 0.1) and is_hit("QTUM", -0.02, 0.1)


def test_outcome_rows_fill_as_prices_mature(engine: Engine) -> None:
    days = seed(engine, date(2025, 7, 15))
    acc = add_conclusion(engine, "ACME", "2025-04-01", "ACCUMULATE", confidence=0.7)
    held = add_conclusion(engine, "ACME", "2025-04-06", "ACCUMULATE", held=True, proposed="AVOID")
    qtum = add_conclusion(engine, "QTUM", "2025-04-01", "HOLD")
    theme = add_conclusion(engine, None, "2025-04-01", "PURE_PLAYS")

    assert run_track_record(engine, P).rows_written == 18  # 3 calls x 6 horizons
    rows = outcomes(engine)
    assert not any(cid == held for cid, _ in rows)  # held updates don't count as calls
    status = {h: rows[(acc, h)]["status"] for h in ("1m", "3m", "6m", "36m")}
    assert status == {"1m": "complete", "3m": "complete", "6m": "pending", "36m": "pending"}

    # 1m by hand: start = last close on/before 2025-04-01, end = on/before 2025-05-01.
    i0, i1 = last_on_or_before(days, "2025-04-01"), last_on_or_before(days, "2025-05-01")
    acme = (1.002**i1) / (1.002**i0) - 1
    qt = (1.001**i1) / (1.001**i0) - 1
    r = rows[(acc, "1m")]
    assert r["start_d"] == days[i0] and r["benchmark"] == "QTUM"
    assert r["excess_return"] == pytest.approx(acme - qt, abs=1e-6)
    assert r["hit"] == 1 and r["hold_hit"] == 1  # small positive excess: inside the HOLD band
    # Momentum: ACME outgrew QTUM over the 90 days before the call -> ACCUMULATE.
    assert r["momentum_stance"] == "ACCUMULATE" and r["momentum_hit"] == 1
    # QTUM's own stance is judged vs QQQ; the theme vs the pure-play basket.
    assert rows[(qtum, "1m")]["benchmark"] == "QQQ"
    basket = ((1.002**i1) / (1.002**i0) + (0.999**i1) / (0.999**i0)) / 2 - 1
    assert rows[(theme, "1m")]["excess_return"] == pytest.approx(basket - qt, abs=1e-6)

    # Nothing new: nothing written.
    assert run_track_record(engine, P).rows_written == 0
    # More sessions arrive: the 6-month rows complete.
    seed(engine, date(2025, 10, 31))
    assert run_track_record(engine, P).rows_written > 0
    assert outcomes(engine)[(acc, "6m")]["status"] == "complete"
    assert outcomes(engine)[(acc, "12m")]["status"] == "pending"


def test_standing_labels_and_proven(engine: Engine) -> None:
    assert standing([], P).label == "No track record yet."
    ids = [add_conclusion(engine, "ACME", f"2025-0{m}-01", "ACCUMULATE") for m in range(1, 10)]
    for cid in ids:
        add_outcome(engine, cid, "6m", hit=1, hold=0, mom=0)
    from aether.score.track_record import outcome_rows_for

    with engine.connect() as conn:
        st = standing(outcome_rows_for(conn, "ACME"), P)
    assert (st.n, st.proven) == (9, False) and st.label.startswith("n too small (<10")
    add_outcome(engine, add_conclusion(engine, "ACME", "2025-10-01", "ACCUMULATE"), "6m", 1, 0, 0)
    with engine.connect() as conn:
        st = standing(outcome_rows_for(conn, "ACME"), P)
    assert (st.n, st.proven, st.hit_rate) == (10, True, 1.0)
    # Not beating a baseline: unproven even with n >= 10.
    add_outcome(engine, add_conclusion(engine, "ACME", "2025-11-01", "ACCUMULATE"), "6m", 0, 1, 1)
    for _ in range(10):
        add_outcome(engine, add_conclusion(engine, "ACME", "2025-12-01", "HOLD"), "6m", 0, 1, 1)
    with engine.connect() as conn:
        st = standing(outcome_rows_for(conn, "ACME"), P)
    assert not st.proven and "does not beat" in st.label


def test_overlay_outcome_rows_fill_as_prices_mature(rw_engine: Engine) -> None:
    cfg = load_strategies(CONFIG_DIR)
    seed_prices(rw_engine, n=120, start=date(2026, 1, 2))
    fake_run(rw_engine, "2026-03-02", {"safe": {"QTUM": 0.75, "ACME": 0.10, "DEMO": 0.15}})
    publish_targets(rw_engine, cfg, today=date(2026, 3, 2), weights=W)
    run_track_record(rw_engine, P)
    with rw_engine.connect() as conn:
        rows = {
            (r.profile, r.horizon): dict(r._mapping) for r in conn.execute(select(overlay_outcomes))
        }
        base = json.loads(
            conn.execute(
                select(profile_targets.c.base_weights).where(profile_targets.c.profile == "safe")
            ).scalar_one()
        )
        px = {
            (r.symbol, r.d): r.c
            for r in conn.execute(select(prices_daily.c.symbol, prices_daily.c.d, prices_daily.c.c))
        }
    one = rows[("safe", "1m")]
    assert one["status"] == "complete" and rows[("safe", "6m")]["status"] == "pending"
    start, end = one["start_d"], max(d for (s, d) in px if s == "QTUM" and d <= "2026-04-02")
    by_hand = sum(w * (px[(s, end)] / px[(s, start)] - 1) for s, w in base.items())
    assert one["base_return"] == pytest.approx(by_hand, abs=1e-6)
    # No overlay adjustments here: adjusted == base.
    assert one["adjusted_return"] == pytest.approx(one["base_return"], abs=1e-9)
    seed_prices(rw_engine, n=200, start=date(2026, 1, 2))
    run_track_record(rw_engine, P)
    with rw_engine.connect() as conn:
        st = conn.execute(
            select(overlay_outcomes.c.status).where(
                overlay_outcomes.c.profile == "safe", overlay_outcomes.c.horizon == "6m"
            )
        ).scalar_one()
    assert st == "complete"
