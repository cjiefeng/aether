"""Options analytics (spec §6.8, M8). Pure functions; research only, never sizing or trades.

- **Skew (30 days):** 25-delta put IV minus 25-delta call IV. Deltas are Black-Scholes deltas from
  each contract's own IV, with r = 0 (the same zero rate as the backtests, §6.5) and
  T = days to expiry / 365. Per expiry, the IV at ±0.25 delta is interpolated linearly in delta
  between the two gated contracts that bracket it; the skew is then interpolated linearly in days
  between the two expiries that bracket 30 days. Nothing is extrapolated: a missing bracket is a
  null with a reason.
- **Implied move** into a catalyst: the at-the-money straddle mid (call mid + put mid at the
  strike nearest the spot listed as both) on the **first listed expiry after the catalyst date**,
  divided by the spot. Both legs must pass the quality gates.
- **History metrics** from Aether's own snapshots: IV rank and percentile of the 30-day ATM IV over
  the last 252 snapshots ("building history" until there are 252), and total volume vs the
  median of the previous 20 snapshots.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from statistics import median
from typing import Any

from aether.config import OptionsConfig
from aether.options.snapshot import DP, passes
from aether.providers.options import Chain, Contract, Expiry

SKEW_DAYS = 30
SKEW_DELTA = 0.25
IV_RANK_WINDOW = 252
VOLUME_WINDOW = 20


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(call: bool, spot: float, strike: float, iv: float, t_years: float) -> float:
    """Black-Scholes delta with r = q = 0."""
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * math.sqrt(t_years))
    return norm_cdf(d1) if call else norm_cdf(d1) - 1.0


def iv_at_delta(
    contracts: Sequence[Contract],
    call: bool,
    spot: float,
    dte: int,
    cfg: OptionsConfig,
) -> tuple[float | None, str | None]:
    """IV at delta +0.25 (calls) or -0.25 (puts), interpolated linearly in delta between the two
    gated contracts that bracket it."""
    target = SKEW_DELTA if call else -SKEW_DELTA
    t = dte / 365.0
    pts: list[tuple[float, float]] = sorted(
        (bs_delta(call, spot, c.strike, c.iv, t), c.iv)
        for c in contracts
        if passes(c, cfg) and c.iv is not None
    )
    if len(pts) < 2:
        return None, "fewer than two gated contracts"
    for (d1, v1), (d2, v2) in pairwise(pts):
        if d1 == target:
            return v1, None
        if d1 < target <= d2:
            if d2 == d1:
                return v2, None
            return v1 + (v2 - v1) * (target - d1) / (d2 - d1), None
    return None, f"{target:+.2f} delta not bracketed by gated contracts"


def expiry_skew(e: Expiry, spot: float, d: date, cfg: OptionsConfig) -> dict[str, Any]:
    dte = (e.expiry - d).days
    put, why_p = iv_at_delta(e.puts, False, spot, dte, cfg)
    call, why_c = iv_at_delta(e.calls, True, spot, dte, cfg)
    out: dict[str, Any] = {
        "expiry": e.expiry.isoformat(),
        "dte": dte,
        "put_25d_iv": None if put is None else round(put, DP),
        "call_25d_iv": None if call is None else round(call, DP),
        "skew": None if put is None or call is None else round(put - call, DP),
    }
    if out["skew"] is None:
        out["reason"] = "; ".join(f"{k}: {w}" for k, w in (("put", why_p), ("call", why_c)) if w)
    return out


def skew_30(chain: Chain, d: date, cfg: OptionsConfig) -> tuple[dict[str, Any], str | None]:
    """30-day 25-delta skew, interpolated in days between the bracketing expiries."""
    if chain.spot is None or chain.spot <= 0:
        return {"value": None, "expiries": []}, "no underlying price"
    rows = [
        expiry_skew(e, chain.spot, d, cfg)
        for e in chain.expiries
        if 0 < (e.expiry - d).days <= cfg.max_days
    ]
    usable = [(r["dte"], r["skew"]) for r in rows if r["skew"] is not None]
    lo = [(t, v) for t, v in usable if t <= SKEW_DAYS]
    hi = [(t, v) for t, v in usable if t >= SKEW_DAYS]
    value: float | None = None
    why: str | None = None
    if not usable:
        why = "no expiry with a 25-delta put and call inside the gated strikes"
    elif not lo:
        why = f"no usable expiry at or before {SKEW_DAYS} days; not extrapolated"
    elif not hi:
        why = f"no usable expiry at or after {SKEW_DAYS} days; not extrapolated"
    else:
        (t1, v1), (t2, v2) = lo[-1], hi[0]
        value = v1 if t1 == t2 else v1 + (v2 - v1) * (SKEW_DAYS - t1) / (t2 - t1)
        value = round(value, DP)
    return {"value": value, "expiries": rows}, why


@dataclass(frozen=True)
class CatalystDate:
    catalyst_id: int | None
    label: str
    d: date


def atm_straddle(e: Expiry, spot: float, cfg: OptionsConfig) -> tuple[dict[str, Any] | None, str]:
    puts = {p.strike: p for p in e.puts}
    both = [c for c in e.calls if c.strike in puts]
    if not both:
        return None, "no strike listed as both call and put"
    call = min(both, key=lambda c: (abs(c.strike - spot), c.strike))
    put = puts[call.strike]
    if not (passes(call, cfg) and passes(put, cfg)):
        return None, "ATM call or put fails the quality gates"
    straddle = (call.bid + call.ask) / 2 + (put.bid + put.ask) / 2  # type: ignore[operator]
    return {
        "strike": call.strike,
        "straddle": round(straddle, DP),
        "move": round(straddle / spot, DP),
    }, ""


def implied_moves(
    chain: Chain, d: date, targets: Sequence[CatalystDate], cfg: OptionsConfig
) -> list[dict[str, Any]]:
    """For each catalyst date: the straddle-implied move to the first listed expiry after it."""
    listed = sorted(chain.listed or tuple(e.expiry for e in chain.expiries))
    fetched = {e.expiry: e for e in chain.expiries}
    out = []
    for c in sorted(targets, key=lambda x: (x.d, x.label)):
        row: dict[str, Any] = {
            "catalyst_id": c.catalyst_id,
            "label": c.label,
            "date": c.d.isoformat(),
            "expiry": None,
            "dte": None,
            "strike": None,
            "straddle": None,
            "move": None,
        }
        after = [x for x in listed if x > c.d]
        if c.d < d:
            row["reason"] = "catalyst date has passed"
        elif not after:
            row["reason"] = "no listed expiry after the catalyst; not extrapolated"
        elif after[0] not in fetched:
            row["expiry"] = after[0].isoformat()
            row["reason"] = f"first expiry after the catalyst ({after[0]}) is not in the snapshot"
        elif chain.spot is None or chain.spot <= 0:
            row["reason"] = "no underlying price"
        else:
            e = fetched[after[0]]
            row["expiry"], row["dte"] = e.expiry.isoformat(), (e.expiry - d).days
            s, why = atm_straddle(e, chain.spot, cfg)
            if s is None:
                row["reason"] = why
            else:
                row.update(s)
        out.append(row)
    return out


def history_metrics(
    atm_iv_30: float | None,
    total_volume: int | None,
    past: Sequence[tuple[float | None, int | None]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """IV rank / percentile and volume vs median from earlier snapshots (oldest first)."""
    reasons: dict[str, str] = {}
    ivs = [v for v, _ in past if v is not None][-(IV_RANK_WINDOW - 1) :]
    out: dict[str, Any] = {
        "iv_history_days": len(ivs) + (1 if atm_iv_30 is not None else 0),
        "iv_rank": None,
        "iv_percentile": None,
        "volume_vs_median": None,
    }
    if atm_iv_30 is None:
        reasons["iv_rank"] = "no 30-day ATM IV today"
    elif out["iv_history_days"] < IV_RANK_WINDOW:
        reasons["iv_rank"] = f"building history ({out['iv_history_days']} days of {IV_RANK_WINDOW})"
    else:
        window = [*ivs, atm_iv_30]
        lo, hi = min(window), max(window)
        out["iv_rank"] = round((atm_iv_30 - lo) / (hi - lo), DP) if hi > lo else None
        if hi <= lo:
            reasons["iv_rank"] = "flat IV history"
        out["iv_percentile"] = round(sum(1 for v in ivs if v < atm_iv_30) / len(ivs), DP)
    vols = [v for _, v in past if v is not None][-VOLUME_WINDOW:]
    if total_volume is None:
        reasons["volume_vs_median"] = "no volume today"
    elif len(vols) < VOLUME_WINDOW:
        reasons["volume_vs_median"] = f"building history ({len(vols)} of {VOLUME_WINDOW} snapshots)"
    else:
        med = median(vols)
        if med > 0:
            out["volume_vs_median"] = round(total_volume / med, DP)
        else:
            reasons["volume_vs_median"] = "median volume is zero"
    return out, reasons
