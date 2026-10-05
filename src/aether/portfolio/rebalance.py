"""Rebalance planner (spec §6.6). Pure: the plan is a function of (holdings, prices, published
targets, settings, config) only, so the same inputs give the same plan. No LLM, no broker
integration: the owner places trades manually.

Money is `Decimal` throughout; weights are floats.

Normal mode: a holding trades only when its drift is ≥ `drift_abs` or ≥ `drift_rel` of its
target weight (a name with a zero target and a position always qualifies), and the trade is at
least `min_trade_usd`. Sells come first; buys then go to the most underweight names, each
limited by the cash left after the estimated turnover cost.

New-cash-only mode: no sells. Cash goes to the most underweight names (no drift band), each buy
at least `min_trade_usd`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any

from aether.config import RebalanceParams

CENT = Decimal("0.01")
MICRO = Decimal("0.000001")
BPS = Decimal(10_000)


@dataclass(frozen=True)
class PlanInputs:
    profile: str
    targets_as_of: str
    strategy_id: str | None
    targets: Mapping[str, float]  # published weights, sum ≈ 1
    positions: Mapping[str, Decimal]  # symbol -> shares (> 0)
    cash: Decimal
    prices: Mapping[str, tuple[str, Decimal]]  # symbol -> (date, last close)
    whole_shares: bool
    new_cash_only: bool
    cost_bps: Decimal
    params: RebalanceParams
    notes: tuple[str, ...] = field(default_factory=tuple)

    def hash_payload(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "targets_as_of": self.targets_as_of,
            "strategy_id": self.strategy_id,
            "targets": dict(sorted(self.targets.items())),
            "positions": {s: str(v) for s, v in sorted(self.positions.items())},
            "cash": str(self.cash),
            "prices": {s: [d, str(p)] for s, (d, p) in sorted(self.prices.items())},
            "whole_shares": self.whole_shares,
            "new_cash_only": self.new_cash_only,
            "cost_bps": str(self.cost_bps),
            "params": self.params.model_dump(mode="json"),
            "notes": list(self.notes),
        }


def _money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _floor_shares(x: Decimal, whole: bool) -> Decimal:
    if x <= 0:
        return Decimal(0)
    return x.quantize(Decimal(1) if whole else MICRO, rounding=ROUND_DOWN)


def build_plan(inp: PlanInputs) -> dict[str, Any]:
    p = inp.params
    min_trade = p.min_trade_usd
    rate = inp.cost_bps / BPS
    symbols = sorted(set(inp.targets) | set(inp.positions))
    unpriced = sorted(s for s in inp.positions if s not in inp.prices)
    priced = [s for s in symbols if s in inp.prices]

    value = {s: inp.positions.get(s, Decimal(0)) * inp.prices[s][1] for s in priced}
    total = sum(value.values(), Decimal(0)) + inp.cash
    rows: dict[str, dict[str, Any]] = {}
    for s in priced:
        tw = Decimal(repr(float(inp.targets.get(s, 0.0))))
        w = value[s] / total if total > 0 else Decimal(0)
        rows[s] = {
            "symbol": s,
            "shares": str(inp.positions.get(s, Decimal(0))),
            "price": str(inp.prices[s][1]),
            "price_date": inp.prices[s][0],
            "value": _money(value[s]),
            "weight": float(w),
            "target": float(tw),
            "target_value": _money(tw * total),
            "drift": float(w - tw),
            "action": None,
            "trade_shares": "0",
            "trade_value": Decimal(0),
            "band": False,
            "unfunded": False,
        }

    def needs_trade(r: dict[str, Any]) -> bool:
        drift, target = abs(float(r["drift"])), float(r["target"])
        if target <= 0:
            return Decimal(r["shares"]) > 0
        return bool(drift >= p.drift_abs - 1e-12 or drift >= p.drift_rel * target - 1e-12)

    for r in rows.values():
        r["band"] = needs_trade(r)

    cash = inp.cash
    sells: list[dict[str, Any]] = []
    if not inp.new_cash_only:
        for s in sorted(rows, key=lambda s: (rows[s]["drift"] * -1, s)):
            r = rows[s]
            if not r["band"] or r["target_value"] >= r["value"]:
                continue
            price, held = inp.prices[s][1], Decimal(r["shares"])
            if r["target"] <= 0:
                qty = held  # close the position entirely, fractions included
            else:
                qty = min(
                    held, _floor_shares((r["value"] - r["target_value"]) / price, inp.whole_shares)
                )
            proceeds = _money(qty * price)
            if qty <= 0 or proceeds < min_trade:
                continue
            r["action"], r["trade_shares"], r["trade_value"] = "sell", str(qty), proceeds
            cash += proceeds - _money(proceeds * rate)
            sells.append(r)

    buys: list[dict[str, Any]] = []
    for s in sorted(rows, key=lambda s: (rows[s]["drift"], s)):
        r = rows[s]
        if r["action"] is not None or r["target_value"] <= r["value"]:
            continue
        if not inp.new_cash_only and not r["band"]:
            continue
        price = inp.prices[s][1]
        want = r["target_value"] - r["value"]
        affordable = cash / (1 + rate)
        qty = _floor_shares(min(want, affordable) / price, inp.whole_shares)
        cost_value = _money(qty * price)
        if qty <= 0 or cost_value < min_trade:
            # Worth a trade, but there isn't enough cash: shown as "needs cash".
            full = _money(_floor_shares(want / price, inp.whole_shares) * price)
            r["unfunded"] = full >= min_trade
            continue
        r["action"], r["trade_shares"], r["trade_value"] = "buy", str(qty), cost_value
        cash -= cost_value + _money(cost_value * rate)
        buys.append(r)

    turnover = sum((r["trade_value"] for r in (*sells, *buys)), Decimal(0))
    est_cost = sum((_money(r["trade_value"] * rate) for r in (*sells, *buys)), Decimal(0))
    sold = sum((r["trade_value"] for r in sells), Decimal(0))
    bought = sum((r["trade_value"] for r in buys), Decimal(0))
    cash_after = inp.cash + sold - bought - est_cost

    for r in rows.values():
        sign = {"sell": -1, "buy": 1}.get(r["action"] or "", 0)
        new_value = r["value"] + sign * r["trade_value"]
        r["post_weight"] = float(new_value / total) if total > 0 else 0.0

    def out(r: dict[str, Any]) -> dict[str, Any]:
        return {
            **r,
            "value": str(r["value"]),
            "target_value": str(r["target_value"]),
            "trade_value": str(r["trade_value"]),
        }

    return {
        "profile": inp.profile,
        "targets_as_of": inp.targets_as_of,
        "strategy_id": inp.strategy_id,
        "mode": "new_cash_only" if inp.new_cash_only else "rebalance",
        "whole_shares": inp.whole_shares,
        "total_value": str(_money(total)),
        "cash": str(_money(inp.cash)),
        "cash_weight": float(inp.cash / total) if total > 0 else 0.0,
        "cash_after": str(_money(cash_after)),
        "cash_weight_after": float(cash_after / total) if total > 0 else 0.0,
        "turnover": str(_money(turnover)),
        "est_cost": str(_money(est_cost)),
        "cost_bps": str(inp.cost_bps),
        "rows": [out(rows[s]) for s in priced],
        "trades": [
            {
                "symbol": r["symbol"],
                "action": r["action"],
                "shares": r["trade_shares"],
                "value": str(r["trade_value"]),
            }
            for r in (*sells, *buys)
        ],
        "unpriced": unpriced,
        "notes": list(inp.notes),
        "band": {
            "drift_abs": p.drift_abs,
            "drift_rel": p.drift_rel,
            "min_trade_usd": str(p.min_trade_usd),
        },
    }


# --------------------------------------------------------------------------- drift (brief)


def drift_summary(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Percentages only (no shares, prices or dollar values): the only holdings-derived data
    that may ever reach synthesis, and only if `positions.share_drift_with_llm` is enabled
    (M10)."""
    out = []
    for r in plan.get("rows", []):
        w, t = float(r["weight"]), float(r["target"])
        out.append(
            {
                "symbol": r["symbol"],
                "weight_pct": round(w * 100, 1),
                "target_pct": round(t * 100, 1),
                "ratio": round(w / t, 2) if t > 0 else None,
            }
        )
    return out


def drift_lines(plan: Mapping[str, Any], min_ratio: float = 1.5) -> list[str]:
    """Weekly-brief lines, e.g. "You are 2.0x overweight <SYM> vs plan (6.0% vs 3.0%)." (M10)."""
    lines = []
    for d in drift_summary(plan):
        ratio, w, t = d["ratio"], d["weight_pct"], d["target_pct"]
        if ratio is None:
            if w > 0:
                lines.append(f"You hold {d['symbol']} ({w:.1f}%) but the plan has 0%.")
        elif ratio >= min_ratio:
            lines.append(
                f"You're {ratio:.1f}× overweight {d['symbol']} vs plan ({w:.1f}% vs {t:.1f}%)."  # noqa: RUF001
            )
        elif ratio <= 1 / min_ratio:
            lines.append(f"You're underweight {d['symbol']} vs plan ({w:.1f}% vs {t:.1f}%).")
    return lines
