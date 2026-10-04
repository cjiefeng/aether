"""Deterministic profile selection (spec §6.5).

1. Qualify: drop candidates that break the profile's limits. Limits are relative to QTUM's own
   out-of-sample result (volatility <= k x QTUM's; max drawdown <= QTUM's + N pp).
2. Rank the rest by the profile's metric (lowest CVaR95, or highest Sortino).
3. Tie-break on max drawdown (smaller first), then strategy ID.

If nothing qualifies, the result says so with the reason. There is never a silent fallback.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aether.config import ProfileParams
from aether.portfolio.metrics import Metrics


def _num(m: Mapping[str, Any], key: str) -> float | None:
    v = m.get(key)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def qualify(metrics: Metrics, qtum: Metrics, params: ProfileParams) -> dict[str, Any]:
    """Per-limit {value, limit, ok} plus an overall `ok`. A limit of None is shown, not applied."""
    checks: dict[str, Any] = {}
    vol, q_vol = _num(metrics, "volatility"), _num(qtum, "volatility")
    if params.vol_limit_x is None or q_vol is None:
        checks["volatility"] = {"value": vol, "limit": None, "ok": True}
    else:
        limit = params.vol_limit_x * q_vol
        checks["volatility"] = {
            "value": vol,
            "limit": limit,
            "ok": vol is not None and vol <= limit,
        }
    dd, q_dd = _num(metrics, "max_drawdown"), _num(qtum, "max_drawdown")
    if params.max_dd_limit_pp is None or q_dd is None:
        checks["max_drawdown"] = {"value": dd, "limit": None, "ok": True}
    else:
        limit = q_dd + params.max_dd_limit_pp / 100.0
        checks["max_drawdown"] = {"value": dd, "limit": limit, "ok": dd is not None and dd <= limit}
    checks["ok"] = all(c["ok"] for c in checks.values())
    return checks


@dataclass(frozen=True)
class Candidate:
    strategy_id: str
    metrics: Metrics
    qualifies: Mapping[str, Any]


RANK_LABELS = {"cvar95_low": "lowest CVaR95", "sortino_high": "highest Sortino"}


def _rank_key(c: Candidate, rank_metric: str) -> tuple[int, float, float, str]:
    if rank_metric == "cvar95_low":
        v = _num(c.metrics, "cvar95")
        primary = v if v is not None else float("inf")
    else:
        v = _num(c.metrics, "sortino")
        primary = -v if v is not None else float("inf")
    dd = _num(c.metrics, "max_drawdown")
    return (
        0 if v is not None else 1,
        primary,
        dd if dd is not None else float("inf"),
        c.strategy_id,
    )


def choose(candidates: Sequence[Candidate], params: ProfileParams) -> dict[str, Any]:
    """{"strategy_id": id | None, "reason": str, "ranking": [ids, best first]}."""
    passing = [c for c in candidates if c.qualifies.get("ok")]
    if not candidates:
        return {"strategy_id": None, "reason": "no candidates were backtested", "ranking": []}
    if not passing:
        failed: dict[str, int] = {}
        for c in candidates:
            for k, v in c.qualifies.items():
                if k != "ok" and not v["ok"]:
                    failed[k] = failed.get(k, 0) + 1
        parts = ", ".join(f"{k.replace('_', ' ')} limit broken by {n}" for k, n in failed.items())
        return {
            "strategy_id": None,
            "reason": f"No qualifying strategy: all {len(candidates)} candidates break the "
            f"profile's limits ({parts}).",
            "ranking": [],
        }
    ranked = sorted(passing, key=lambda c: _rank_key(c, params.rank_metric))
    best = ranked[0]
    return {
        "strategy_id": best.strategy_id,
        "reason": f"{len(passing)} of {len(candidates)} candidates qualify; ranked by "
        f"{RANK_LABELS[params.rank_metric]}, ties broken by max drawdown, then ID.",
        "ranking": [c.strategy_id for c in ranked],
    }
