"""M5 acceptance: the rebalance plan is deterministic, respects the no-trade band and the
minimum trade, lists sells before buys, and its drift output carries percentages only."""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

from aether.config import load_strategies
from aether.portfolio.job import canon
from aether.portfolio.publish import plan_hash
from aether.portfolio.rebalance import PlanInputs, build_plan, drift_lines, drift_summary
from tests.conftest import CONFIG_DIR

CONFIG = load_strategies(CONFIG_DIR)
D = Decimal


def inputs(**kw: object) -> PlanInputs:
    base = PlanInputs(
        profile="medium",
        targets_as_of="2026-10-02",
        strategy_id="medium:core_equal:q50",
        targets={"QTUM": 0.5, "ACME": 0.2, "DEMO": 0.2, "EXMP": 0.1},
        positions={"QTUM": D(100), "ACME": D(300), "FAKE": D(10)},
        cash=D("5000"),
        prices={
            "QTUM": ("2026-10-02", D("100")),
            "ACME": ("2026-10-02", D("20")),
            "DEMO": ("2026-10-02", D("50")),
            "EXMP": ("2026-10-02", D("10")),
            "FAKE": ("2026-10-02", D("30")),
        },
        whole_shares=True,
        new_cash_only=False,
        cost_bps=D("10"),
        params=CONFIG.rebalance,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_same_inputs_give_identical_plan_and_hash() -> None:
    a, b = build_plan(inputs()), build_plan(inputs())
    assert canon(a) == canon(b)
    assert plan_hash(inputs()) == plan_hash(inputs())
    assert plan_hash(inputs()) != plan_hash(inputs(cash=D("5001")))


def test_sells_first_then_buys_and_cash_never_negative() -> None:
    plan = build_plan(inputs())
    actions = [t["action"] for t in plan["trades"]]
    assert actions == sorted(actions, key=lambda a: a != "sell")  # all sells before buys
    assert "sell" in actions and "buy" in actions
    assert D(plan["cash_after"]) >= 0
    # FAKE has a zero target: the whole position is sold.
    fake = next(t for t in plan["trades"] if t["symbol"] == "FAKE")
    assert fake == {"symbol": "FAKE", "action": "sell", "shares": "10", "value": "300.00"}
    # Whole shares only.
    assert all(D(t["shares"]) == D(t["shares"]).to_integral_value() for t in plan["trades"])
    # Cash bookkeeping: start + sells - buys - cost.
    sold = sum(D(t["value"]) for t in plan["trades"] if t["action"] == "sell")
    bought = sum(D(t["value"]) for t in plan["trades"] if t["action"] == "buy")
    assert D(plan["cash_after"]) == D(plan["cash"]) + sold - bought - D(plan["est_cost"])
    assert D(plan["est_cost"]) == (D(plan["turnover"]) * D("0.001")).quantize(D("0.01"))


def test_no_trade_below_minimum() -> None:
    # Tiny portfolio: every drift is large, but no trade reaches $100.
    plan = build_plan(
        inputs(
            positions={"QTUM": D(1)},
            cash=D("150"),
            prices={
                "QTUM": ("2026-10-02", D("40")),
                "ACME": ("2026-10-02", D("60")),
                "DEMO": ("2026-10-02", D("60")),
                "EXMP": ("2026-10-02", D("60")),
            },
        )
    )
    assert plan["trades"] == []
    assert any(r["band"] for r in plan["rows"])
    for seed_cash in ("0", "99.99", "250", "1000"):
        p = build_plan(inputs(cash=D(seed_cash)))
        assert all(D(t["value"]) >= CONFIG.rebalance.min_trade_usd for t in p["trades"])


def test_inside_band_no_trade() -> None:
    # Weights exactly on target except a 2 pp drift on a 50% name (< 3 pp and < 25%).
    prices = {s: ("2026-10-02", D("10")) for s in ("QTUM", "ACME")}
    plan = build_plan(
        inputs(
            targets={"QTUM": 0.5, "ACME": 0.5},
            positions={"QTUM": D(5200), "ACME": D(4800)},
            cash=D(0),
            prices=prices,
        )
    )
    assert plan["trades"] == []
    assert not any(r["band"] for r in plan["rows"])


def test_relative_band_triggers_small_target() -> None:
    # ACME target 4%, held ~2.4%: drift < 3 pp but > 25% of its target, so it qualifies.
    # QTUM's drift is inside the band, so it isn't sold to fund it: the buy needs cash.
    prices = {s: ("2026-10-02", D("10")) for s in ("QTUM", "ACME")}
    kw = {
        "targets": {"QTUM": 0.96, "ACME": 0.04},
        "positions": {"QTUM": D(975), "ACME": D(25)},
        "prices": prices,
    }
    unfunded = build_plan(inputs(cash=D(0), **kw))
    assert unfunded["trades"] == []
    acme = next(r for r in unfunded["rows"] if r["symbol"] == "ACME")
    assert acme["band"] and acme["unfunded"]
    qtum = next(r for r in unfunded["rows"] if r["symbol"] == "QTUM")
    assert not qtum["band"]
    funded = build_plan(inputs(cash=D(400), **kw))
    assert [(t["symbol"], t["action"]) for t in funded["trades"]] == [("ACME", "buy")]


def test_new_cash_only_never_sells() -> None:
    plan = build_plan(inputs(new_cash_only=True))
    assert plan["mode"] == "new_cash_only"
    assert all(t["action"] == "buy" for t in plan["trades"])
    assert plan["trades"]  # cash is put to work
    # Most underweight first: DEMO (0% vs 20%) before EXMP (0% vs 10%).
    order = [t["symbol"] for t in plan["trades"]]
    assert order.index("DEMO") < order.index("EXMP")
    assert D(plan["cash_after"]) >= 0


def test_fractional_shares_mode() -> None:
    plan = build_plan(inputs(whole_shares=False))
    assert any(D(t["shares"]) != D(t["shares"]).to_integral_value() for t in plan["trades"])


def test_unpriced_holding_is_reported_not_guessed() -> None:
    prices = dict(inputs().prices)
    del prices["FAKE"]
    plan = build_plan(inputs(prices=prices))
    assert plan["unpriced"] == ["FAKE"]
    assert "FAKE" not in {r["symbol"] for r in plan["rows"]}


def test_drift_output_has_percentages_only() -> None:
    plan = build_plan(inputs())
    summary = drift_summary(plan)
    assert {k for d in summary for k in d} == {"symbol", "weight_pct", "target_pct", "ratio"}
    lines = drift_lines(plan)
    assert lines
    text = " ".join(lines)
    assert "$" not in text
    # No share counts or dollar values from the plan appear in the lines.
    for r in plan["rows"]:
        for v in (r["shares"], r["value"], r["trade_value"]):
            if D(v) >= 100:
                assert not re.search(rf"\b{re.escape(str(v))}\b", text)
