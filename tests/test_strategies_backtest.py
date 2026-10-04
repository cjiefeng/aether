"""Strategy families, caps and the walk-forward backtest (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from aether.config import BacktestParams, load_strategies
from aether.portfolio.backtest import oos_start, run_backtest
from aether.portfolio.strategies import (
    FAMILIES,
    cap_weights,
    eligible,
    min_variance,
    project_capped_simplex,
    target_weights,
)
from aether.portfolio.total_return import align, simple_returns, total_return_levels
from tests.conftest import CONFIG_DIR
from tests.portfolio_data import panel

PARAMS: BacktestParams = load_strategies(CONFIG_DIR).backtest
COLS = ["QTUM", "ACME", "DEMO", "EXMP", "FAKE"]


def _R(closes: dict[str, list[tuple[str, float]]]) -> tuple[np.ndarray, list[str]]:
    cal = [d for d, _ in closes["QTUM"]]
    lv = np.column_stack([align(total_return_levels(closes[s], {}), cal) for s in COLS])
    return simple_returns(lv), cal


def test_cap_weights_redistributes_and_overflows() -> None:
    w = cap_weights(np.array([3.0, 1.0, 1.0]), 0.5, 0.25)
    assert w == pytest.approx([0.25, 0.125, 0.125])
    w = cap_weights(np.ones(3), 0.2, 0.05)
    assert w == pytest.approx([0.05] * 3)  # only 0.15 placed; the rest goes to QTUM
    assert cap_weights(np.zeros(3), 0.5, 0.2).sum() == 0


def test_capped_overflow_goes_to_qtum() -> None:
    R, _ = _R(panel())
    w = target_weights(
        "core_momentum",
        R,
        200,
        core=0,
        sleeve=[1, 2, 3, 4],
        qtum_weight=0.8,
        cap=0.05,
        params=PARAMS,
    )
    assert w.sum() == pytest.approx(1.0)
    assert np.count_nonzero(w[1:]) == 3 and w[1:].max() == pytest.approx(0.05)
    assert w[0] == pytest.approx(0.85)  # 0.8 core + 0.05 the caps couldn't place


def test_projection_onto_capped_simplex() -> None:
    p = project_capped_simplex(np.array([0.9, 0.1, -0.3, 0.2]), 0.6, 0.3)
    assert p.sum() == pytest.approx(0.6, abs=1e-9)
    assert p.min() >= 0 and p.max() <= 0.3 + 1e-12
    assert project_capped_simplex(np.zeros(3), 1.0, 0.2) == pytest.approx([0.2] * 3)


def test_min_variance_matches_two_asset_closed_form() -> None:
    s1, s2, rho = 0.04, 0.01, 0.3
    c12 = rho * np.sqrt(s1 * s2)
    cov = np.array([[s1, c12], [c12, s2]])
    w = min_variance(cov, 1.0, 1.0, 2000)
    w1 = (s2 - c12) / (s1 + s2 - 2 * c12)
    assert w == pytest.approx([w1, 1 - w1], abs=1e-6)
    capped = min_variance(cov, 1.0, 0.6, 2000)  # unconstrained w2 ~ 0.85 > cap
    assert capped == pytest.approx([0.4, 0.6], abs=1e-6)


def test_momentum_picks_top_three_by_trailing_return() -> None:
    R = np.full((200, 5), 0.0)
    R[0] = np.nan
    R[1:, 1:] = [0.001, 0.004, 0.002, 0.003]  # ranks: DEMO, FAKE, EXMP, ACME
    w = target_weights(
        "core_momentum",
        R,
        199,
        core=0,
        sleeve=[1, 2, 3, 4],
        qtum_weight=0.4,
        cap=0.35,
        params=PARAMS,
    )
    assert w[1] == 0 and w[2:] == pytest.approx([0.2] * 3)


def test_names_join_after_min_sessions() -> None:
    R = np.full((100, 3), 0.001)
    R[0] = np.nan
    R[:40, 2] = np.nan  # column 2's first return is at row 40
    # Before session t only R[:t] counts: at t=100 col 2 has 60 returns, at t=99 it has 59.
    assert eligible(R, 100, [1, 2], 60) == [1, 2]
    assert eligible(R, 99, [1, 2], 60) == [1]


@pytest.mark.parametrize("family", FAMILIES)
def test_no_look_ahead(family: str) -> None:
    closes = panel(late={"FAKE": 100})
    R, cal = _R(closes)
    k = 190
    bumped = {s: list(v) for s, v in closes.items()}
    for s in COLS:
        i = next(i for i, (d, _) in enumerate(bumped[s]) if d == cal[k])
        d, c = bumped[s][i]
        bumped[s][i] = (d, c * 1.5)  # perturb day-k prices only
    R2, _ = _R(bumped)
    kw = {"core": 0, "sleeve": [1, 2, 3, 4], "qtum_weight": 0.5, "cap": 0.35, "params": PARAMS}
    for t in range(oos_start(PARAMS), k + 1):  # weights held on sessions <= k
        a = target_weights(family, R, t, **kw)  # type: ignore[arg-type]
        b = target_weights(family, R2, t, **kw)  # type: ignore[arg-type]
        assert np.array_equal(a, b), t
    changed = target_weights(family, R2, k + 1, **kw)  # type: ignore[arg-type]
    assert changed.shape == (5,)
    res1 = run_backtest(family, R, cal, **kw)  # type: ignore[arg-type]
    res2 = run_backtest(family, R2, cal, **kw)  # type: ignore[arg-type]
    for t, w in res1.applied.items():
        if t <= k:
            assert np.array_equal(w, res2.applied[t])
    t0 = oos_start(PARAMS)
    assert np.array_equal(res1.returns[: k - t0], res2.returns[: k - t0])


def test_cost_and_rebalance_schedule() -> None:
    R, cal = _R(panel())
    res = run_backtest(
        "core_equal",
        R,
        cal,
        core=0,
        sleeve=[1, 2, 3, 4],
        qtum_weight=1.0,
        cap=0.35,
        params=PARAMS,
    )
    t0 = oos_start(PARAMS)
    # All QTUM: the initial allocation costs 10 bps, later rebalances trade nothing.
    assert res.returns[0] == pytest.approx(R[t0, 0] - 0.001)
    assert res.returns[1:] == pytest.approx(R[t0 + 1 :, 0])
    assert res.turnovers[0] == pytest.approx(1.0) and max(res.turnovers[1:]) < 1e-12
    for t in res.rebalance_days[1:]:
        assert cal[t][:7] != cal[t - 1][:7]  # first session of a month
    months = {d[:7] for d in cal[t0:]}
    assert len(res.rebalance_days) == len(months)


def test_backtest_weights_sum_to_one_and_respect_caps() -> None:
    R, cal = _R(panel(late={"FAKE": 150}))
    for family in FAMILIES:
        res = run_backtest(
            family,
            R,
            cal,
            core=0,
            sleeve=[1, 2, 3, 4],
            qtum_weight=0.5,
            cap=0.15,
            params=PARAMS,
        )
        for w in [*res.applied.values(), res.current]:
            assert w.sum() == pytest.approx(1.0)
            assert w[1:].max() <= 0.15 + 1e-9 and w.min() >= -1e-12
            assert w[0] >= 0.5 - 1e-9
