"""M9 acceptance: the event-reaction engine (spec §6.3).

After-close anchoring, holidays, the beta fallback, confounding, pending -> complete, and a
synthetic +10% jump giving z1 > 2. Prices are synthetic on real NYSE sessions; symbols other than
the benchmarks the code keys on (QTUM/QQQ) are made up.
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest
from sqlalchemy import Engine, delete, select

from aether import nyse
from aether.config import load_weights
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import event_reactions, events, prices_daily
from aether.db.types import utcnow_iso
from aether.score.prices import PriceSeries
from aether.score.reaction import anchor, compute, is_approx_time, run_reactions
from tests.conftest import CONFIG_DIR
from tests.holdings_data import add_event, seed_universe

P = load_weights(CONFIG_DIR).reactions


# --------------------------------------------------------------------------- anchoring


@pytest.mark.parametrize(
    ("published", "t0"),
    [
        ("2026-03-10T19:59:00Z", "2026-03-10"),  # 15:59 EDT, before the close
        ("2026-03-10T20:00:00Z", "2026-03-11"),  # exactly at the close: not after it
        ("2026-03-10T20:30:00Z", "2026-03-11"),  # 16:30 EDT, after the close
        ("2026-02-10T20:30:00Z", "2026-02-10"),  # 15:30 EST (close is 21:00Z in winter)
        ("2025-11-28T18:30:00Z", "2025-12-01"),  # 13:30 ET on a half-day (close 13:00)
        ("2025-11-28T17:30:00Z", "2025-11-28"),  # 12:30 ET on the half-day
        ("2026-01-16T21:30:00Z", "2026-01-20"),  # Fri after close; Mon is MLK Day
        ("2026-07-04T14:00:00Z", "2026-07-06"),  # Saturday; Fri 07-03 is the observed holiday
    ],
)
def test_anchor_after_close_holidays_and_half_days(published: str, t0: str) -> None:
    assert anchor(published) == date.fromisoformat(t0)


def test_approx_time_flags_research_dates_only() -> None:
    assert is_approx_time("web_search", {"date_source": "retrieved"}, "2026-03-10T08:15:00Z")
    assert is_approx_time("web_search", {"date_source": "page_age"}, "2026-03-10T12:00:00Z")
    assert not is_approx_time("web_search", {"date_source": "page_age"}, "2026-03-10T13:05:00Z")
    assert not is_approx_time("edgar", None, "2026-03-10T12:00:00Z")


# --------------------------------------------------------------------------- pure maths


def series(
    sym: str, days: list[str], rets: np.ndarray, volume: int = 1000, jump: dict | None = None
) -> PriceSeries:
    lv = [1.0]
    for r in rets[1:]:
        lv.append(lv[-1] * (1.0 + r))
    vols = [volume] * len(days)
    for d, v in (jump or {}).items():
        vols[days.index(d)] = v
    return PriceSeries(sym, tuple(days), tuple(lv), tuple(lv), tuple(vols))


def market(
    n: int, end: date, seed: int = 1, beta: float = 1.3
) -> tuple[list[str], np.ndarray, np.ndarray]:
    days = [d.isoformat() for d in nyse.sessions(date(2024, 1, 1), end)][-n:]
    rng = np.random.default_rng(seed)
    rb = rng.normal(0.0005, 0.01, n)
    rs = beta * rb + rng.normal(0, 0.015, n)
    return days, rb, rs


def test_jump_gives_z1_above_2_and_beta_is_estimated() -> None:
    days, rb, rs = market(170, date(2026, 6, 30))
    t0 = days[140]
    rs = rs.copy()
    rs[140] += 0.10  # synthetic +10% abnormal jump on t0
    m = compute(series("ACME", days, rs), series("QTUM", days, rb), date.fromisoformat(t0), P)
    assert m["beta_fallback"] == 0
    assert m["beta"] == pytest.approx(1.3, abs=0.25)
    assert m["z_1"] > 2
    assert m["car_1"] == pytest.approx(0.10, abs=0.06)
    assert m["filled"] and m["car_20"] is not None and m["z_20"] is not None


def test_beta_fallback_with_short_history() -> None:
    days, rb, rs = market(70, date(2026, 6, 30))
    t0 = date.fromisoformat(days[40])  # 39 returns before t0 (< 60)
    m = compute(series("ACME", days, rs), series("QTUM", days, rb), t0, P)
    assert m["beta_fallback"] == 1 and m["beta"] == 1.0
    assert m["sigma_resid"] is not None and m["z_1"] is not None


def test_too_short_for_sigma_has_no_z() -> None:
    days, rb, rs = market(40, date(2026, 6, 30))
    m = compute(series("ACME", days, rs), series("QTUM", days, rb), date.fromisoformat(days[5]), P)
    assert m["beta_fallback"] == 1 and m["z_1"] is None and m["car_1"] is not None
    assert "no z-scores" in m["note"]


def test_window_fills_only_when_both_series_have_the_bar() -> None:
    days, rb, rs = market(150, date(2026, 6, 30))
    t0 = date.fromisoformat(days[140])  # 9 sessions after t0 exist
    m = compute(series("ACME", days, rs), series("QTUM", days, rb), t0, P)
    assert m["car_1"] is not None and m["car_5"] is not None
    assert m["car_20"] is None and not m["filled"]
    # The benchmark lags one session: t0+5 isn't filled until it has that bar too.
    short = series("QTUM", days[:145], rb[:145])
    m2 = compute(series("ACME", days, rs), short, t0, P)
    assert m2["car_5"] is None and m2["car_1"] is not None


def test_abnormal_volume_and_reversal() -> None:
    days, rb, rs = market(170, date(2026, 6, 30))
    rs = rs.copy()
    rs[140] += 0.10
    rs[150] -= 0.10  # the pop fades
    s = series("ACME", days, rs, volume=1000, jump={days[140]: 5000})
    m = compute(s, series("QTUM", days, rb), date.fromisoformat(days[140]), P)
    assert m["abn_volume"] == pytest.approx(5.0)
    assert m["reversal_ratio"] is not None and m["reversal_ratio"] < 0.5


def test_event_before_listing_is_no_data() -> None:
    days, rb, rs = market(100, date(2026, 6, 30))
    m = compute(
        series("ACME", days[50:], rs[50:]),
        series("QTUM", days, rb),
        date.fromisoformat(days[10]),
        P,
    )
    assert m["no_data"]


# --------------------------------------------------------------------------- job (DB)


def seed(engine: Engine, days: list[str], closes: dict[str, list[float]]) -> None:
    seed_universe(engine)
    rows = [
        {
            "symbol": s,
            "d": d,
            "o": c,
            "h": c,
            "l": c,
            "c": c,
            "volume": 1000,
            "provider": "synthetic",
            "fetched_at": utcnow_iso(),
        }
        for s, cs in closes.items()
        for d, c in zip(days, cs, strict=True)
    ]
    with write_tx(engine) as conn:
        upsert(conn, prices_daily, rows, key_cols=["symbol", "d"])


def closes_from(rets: np.ndarray, start: float = 50.0) -> list[float]:
    return [round(float(x), 6) for x in start * np.cumprod(1.0 + rets)]


def reactions(engine: Engine) -> dict[tuple[int, str], dict]:
    with engine.connect() as conn:
        return {
            (r.event_id, r.symbol): dict(r._mapping) for r in conn.execute(select(event_reactions))
        }


def test_job_pending_then_complete_and_confounded(rw_engine: Engine) -> None:
    days, rb, rs = market(200, date(2026, 6, 30))
    rs = rs.copy()
    rs[150] += 0.10
    other = rb + np.random.default_rng(9).normal(0, 0.02, len(rb))
    full = {"QTUM": closes_from(rb), "ACME": closes_from(rs), "DEMO": closes_from(other)}
    # Only the first 160 sessions are stored at first: the [t0, t0+20] window isn't filled.
    seed(rw_engine, days[:160], {k: v[:160] for k, v in full.items()})
    t0 = days[150]
    ts = f"{t0}T14:00:00Z"  # 10:00 ET: t0 is that day
    e1 = add_event(
        rw_engine, "ACME", ts, materiality=2, cls="SIGNAL", category="contract_with_value"
    )
    # DEMO: a second, material event three sessions later confounds the first.
    e2 = add_event(
        rw_engine, "DEMO", ts, materiality=2, cls="SIGNAL", category="contract_with_value"
    )
    add_event(
        rw_engine, "DEMO", f"{days[153]}T14:00:00Z", materiality=3, cls="SIGNAL", category="m_and_a"
    )
    # Quarantined events get no reaction row.
    eq = add_event(rw_engine, "ACME", ts, materiality=5, quarantined=True)

    p = P
    assert run_reactions(rw_engine, p).rows_written == 3  # e1, e2 and the DEMO M&A event
    r = reactions(rw_engine)
    assert (eq, "ACME") not in r
    assert r[(e1, "ACME")]["status"] == "pending" and r[(e1, "ACME")]["t0"] == t0
    assert r[(e1, "ACME")]["z_1"] > 2 and r[(e1, "ACME")]["car_20"] is None
    assert r[(e2, "DEMO")]["status"] == "confounded"
    assert len(json.loads(r[(e2, "DEMO")]["confounders"])) == 1

    # Re-running with nothing new writes nothing.
    assert run_reactions(rw_engine, p).rows_written == 0

    # More sessions arrive: the ACME row completes.
    seed(rw_engine, days, full)
    run_reactions(rw_engine, p)
    r = reactions(rw_engine)
    assert r[(e1, "ACME")]["status"] == "complete"
    assert r[(e1, "ACME")]["car_20"] is not None and r[(e1, "ACME")]["reversal_ratio"] is not None
    assert r[(e2, "DEMO")]["status"] == "confounded"


def test_qtum_event_uses_qqq_and_deleted_event_cascades(rw_engine: Engine) -> None:
    days, rb, rs = market(200, date(2026, 6, 30))
    seed(rw_engine, days, {"QQQ": closes_from(rb), "QTUM": closes_from(rs)})
    eid = add_event(
        rw_engine,
        "QTUM",
        f"{days[120]}T14:00:00Z",
        cls="NOISE",
        category="analyst_rating",
        materiality=1,
    )
    run_reactions(rw_engine, P)
    row = reactions(rw_engine)[(eid, "QTUM")]
    assert row["benchmark"] == "QQQ" and row["status"] == "complete"
    with write_tx(rw_engine) as conn:
        conn.execute(delete(events).where(events.c.id == eid))
    assert reactions(rw_engine) == {}
