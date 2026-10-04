"""Deterministic profile selection: limits relative to QTUM, ranking, tie-breaks, no fallback."""

from __future__ import annotations

import pytest

from aether.config import ProfileParams
from aether.portfolio.select import Candidate, choose, qualify

QTUM = {"volatility": 0.30, "max_drawdown": 0.25}
SAFE = ProfileParams(
    min_qtum=0.8,
    max_per_name=0.05,
    vol_limit_x=1.15,
    max_dd_limit_pp=5,
    rank_metric="cvar95_low",
    qtum_grid=(0.8,),
)
AGGR = ProfileParams(
    min_qtum=0.0,
    max_per_name=0.35,
    vol_limit_x=None,
    max_dd_limit_pp=None,
    rank_metric="sortino_high",
    qtum_grid=(0.0,),
)


def _c(sid: str, params: ProfileParams, **m: float) -> Candidate:
    metrics = {"volatility": 0.3, "max_drawdown": 0.25, "cvar95": 0.03, "sortino": 1.0, **m}
    return Candidate(sid, metrics, qualify(metrics, QTUM, params))


def test_limits_are_relative_to_qtum() -> None:
    q = qualify({"volatility": 0.345, "max_drawdown": 0.30}, QTUM, SAFE)
    assert q["volatility"]["limit"] == pytest.approx(0.345) and q["volatility"]["ok"]
    assert q["max_drawdown"]["limit"] == pytest.approx(0.30) and q["max_drawdown"]["ok"]
    assert q["ok"]
    q = qualify({"volatility": 0.36, "max_drawdown": 0.30}, QTUM, SAFE)
    assert not q["volatility"]["ok"] and not q["ok"]
    q = qualify({"volatility": 9.0, "max_drawdown": 0.9}, QTUM, AGGR)
    assert q["ok"] and q["volatility"]["limit"] is None


def test_candidate_breaking_a_limit_is_never_selected() -> None:
    # The rule-breaker has by far the best CVaR, but breaks the drawdown limit.
    cands = [
        _c("a", SAFE, cvar95=0.001, max_drawdown=0.31),
        _c("b", SAFE, cvar95=0.02),
        _c("c", SAFE, cvar95=0.03),
    ]
    out = choose(cands, SAFE)
    assert out["strategy_id"] == "b"
    assert out["ranking"] == ["b", "c"]
    assert "2 of 3" in out["reason"]


def test_no_qualifying_strategy_has_a_reason() -> None:
    cands = [_c("a", SAFE, volatility=0.5), _c("b", SAFE, max_drawdown=0.5)]
    out = choose(cands, SAFE)
    assert out["strategy_id"] is None and out["ranking"] == []
    assert out["reason"].startswith("No qualifying strategy")
    assert "volatility limit broken by 1" in out["reason"]
    assert "max drawdown limit broken by 1" in out["reason"]
    assert choose([], SAFE)["strategy_id"] is None


def test_sortino_ranking_and_tie_breaks() -> None:
    cands = [
        _c("z", AGGR, sortino=2.0, max_drawdown=0.4),
        _c("y", AGGR, sortino=2.0, max_drawdown=0.3),  # same Sortino, smaller DD wins
        _c("x", AGGR, sortino=2.0, max_drawdown=0.3),  # full tie: ID decides
        _c("w", AGGR, sortino=1.0),
    ]
    out = choose(cands, AGGR)
    assert out["ranking"] == ["x", "y", "z", "w"]
    assert choose(list(reversed(cands)), AGGR) == out  # order-independent
