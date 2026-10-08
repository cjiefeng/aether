"""M14 minimum weight per name (spec §6.5): every eligible sleeve name gets at least the profile's
floor; the family allocates only the rest; the overlay can still cut a name to 0."""

from __future__ import annotations

import numpy as np
import pytest

from aether.config import load_strategies
from aether.portfolio.overlay import Finding, apply_overlay
from aether.portfolio.strategies import FAMILIES, effective_floor, target_weights
from aether.portfolio.total_return import align, simple_returns, total_return_levels
from tests.conftest import CONFIG_DIR
from tests.portfolio_data import panel

CFG = load_strategies(CONFIG_DIR)
PARAMS = CFG.backtest
COLS = ["QTUM", "ACME", "DEMO", "EXMP", "FAKE"]


def _R() -> np.ndarray:
    closes = panel()
    cal = [d for d, _ in closes["QTUM"]]
    lv = np.column_stack([align(total_return_levels(closes[s], {}), cal) for s in COLS])
    return simple_returns(lv)


@pytest.mark.parametrize("profile", ["safe", "medium", "aggressive"])
@pytest.mark.parametrize("family", FAMILIES)
def test_every_name_within_floor_and_cap_and_sleeve_sums(profile: str, family: str) -> None:
    R = _R()
    pp = CFG.profiles[profile]  # type: ignore[index]
    w = target_weights(
        family,  # type: ignore[arg-type]
        R,
        R.shape[0],
        core=0,
        sleeve=[1, 2, 3, 4],
        qtum_weight=pp.qtum_weight,
        cap=pp.max_per_name,
        params=PARAMS,
        floor=pp.min_per_name,
    )
    sleeve = w[1:]
    assert (sleeve >= pp.min_per_name - 1e-12).all()
    assert (sleeve <= pp.max_per_name + 1e-12).all()
    assert w.sum() == pytest.approx(1.0)
    # The sleeve still sums to its target where the caps allow it (4 names x cap >= sleeve).
    if 4 * pp.max_per_name >= 1 - pp.qtum_weight:
        assert sleeve.sum() == pytest.approx(1 - pp.qtum_weight)
        assert w[0] == pytest.approx(pp.qtum_weight)


def test_momentum_non_top_names_sit_at_the_floor() -> None:
    R = _R()
    pp = CFG.profiles["medium"]
    params = PARAMS.model_copy(update={"momentum_top_n": 2})
    w = target_weights(
        "core_momentum",
        R,
        R.shape[0],
        core=0,
        sleeve=[1, 2, 3, 4],
        qtum_weight=pp.qtum_weight,
        cap=pp.max_per_name,
        params=params,
        floor=pp.min_per_name,
    )
    sleeve = sorted(w[1:].tolist())
    assert sleeve[0] == pytest.approx(pp.min_per_name)
    assert sleeve[1] == pytest.approx(pp.min_per_name)
    assert sleeve[2] > pp.min_per_name and sleeve[3] > pp.min_per_name


def test_floor_shrinks_to_sleeve_over_names_when_it_does_not_fit() -> None:
    assert effective_floor(4, 0.10, 0.04) == pytest.approx(0.025)
    assert effective_floor(4, 0.55, 0.03) == 0.03
    assert effective_floor(0, 0.55, 0.03) == 0.0
    R = _R()
    w = target_weights(
        "core_min_var",
        R,
        R.shape[0],
        core=0,
        sleeve=[1, 2, 3, 4],
        qtum_weight=0.90,
        cap=0.35,
        params=PARAMS,
        floor=0.04,
    )
    assert w[1:] == pytest.approx([0.025] * 4)
    assert w[0] == pytest.approx(0.90)


def test_no_floor_keeps_the_m4_behaviour() -> None:
    R = _R()
    kw = {"core": 0, "sleeve": [1, 2, 3, 4], "qtum_weight": 0.45, "cap": 0.2, "params": PARAMS}
    a = target_weights("core_momentum", R, R.shape[0], **kw)  # type: ignore[arg-type]
    assert (a[1:] == 0).sum() == 1  # top 3 of 4: one name at zero without a floor


def test_overlay_hard_rule_still_zeroes_a_floored_name() -> None:
    base = {"QTUM": 0.45, "ACME": 0.03, "DEMO": 0.20, "EXMP": 0.20, "FAKE": 0.12}
    gc = Finding("ACME", "going_concern", 0.0, "0009999999-26-000001", "10-Q", "2026-08-01", "u", 7)
    published, chain = apply_overlay(base, ["ACME", "DEMO", "EXMP", "FAKE"], 0.20, [gc])
    assert "ACME" not in published  # 0, not the floor
    assert sum(published.values()) == pytest.approx(1.0)
    acme = next(c for c in chain if c["symbol"] == "ACME")
    assert acme["final"] == 0.0 and acme["steps"][0]["rule"] == "going_concern"


def test_strategy_job_reports_the_floor(rw_engine) -> None:  # type: ignore[no-untyped-def]
    from aether.portfolio.job import compute_run, load_inputs
    from tests.portfolio_data import seed_panel

    seed_panel(rw_engine, panel())
    out = compute_run(load_inputs(rw_engine), CFG)
    assert out is not None
    for p in ("safe", "medium", "aggressive"):
        f = out.summary["profiles"][p]["floor"]
        assert f["configured"] == CFG.profiles[p].min_per_name  # type: ignore[index]
        assert f["eligible"] == 4 and f["note"] is None
    shrunk = CFG.model_copy(
        update={
            "profiles": {
                **CFG.profiles,
                "safe": CFG.profiles["safe"].model_copy(update={"min_per_name": 0.08}),
            }
        }
    )
    out = compute_run(load_inputs(rw_engine), shrunk)
    assert out is not None
    note = out.summary["profiles"]["safe"]["floor"]["note"]
    assert note and "shrank from 8.0% to 6.25%" in note
