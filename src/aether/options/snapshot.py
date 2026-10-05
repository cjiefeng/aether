"""Daily options summary metrics (spec §6.8, M5 snapshot; M8 analytics in options/analytics.py).
Pure.

Metrics (summary only, never full chains):
- per listed expiry within `max_days`: the at-the-money strike (nearest the spot, listed as both a
  call and a put) and its IV, from the contracts that pass the quality gates;
- ATM IV at `term_days` (30/60/90), interpolated in total variance between the two nearest
  expiries with an ATM IV; never extrapolated beyond the listed expiries;
- positioning: put/call volume and open-interest ratios over the considered expiries.

Quality gates: a contract is used for IV only with open interest ≥ `min_open_interest`, a two-
sided quote with (ask - bid)/mid ≤ `max_spread_pct` and a positive IV. A metric that can't be
computed is stored as null with a reason; a chain where no ATM contract passes is flagged thin.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING, Any

from aether.config import OptionsConfig
from aether.providers.options import Chain, Contract

if TYPE_CHECKING:
    from aether.options.analytics import CatalystDate

DP = 6


def choose_expiries(
    listed: list[date], d: date, cfg: OptionsConfig, catalyst_dates: Sequence[date] = ()
) -> list[date]:
    """The nearest expiry plus, for each horizon in `term_days`, the listed expiries just before
    and just after it (so each horizon can be interpolated), within `max_days`, at most
    `max_expiries`. M8: plus the first listed expiry after each upcoming catalyst date (for the
    implied move), which doesn't count against `max_expiries`."""
    cands = sorted(e for e in listed if 0 < (e - d).days <= cfg.max_days)
    if not cands:
        return []
    picked = {cands[0]}
    for t in cfg.term_days:
        before = [e for e in cands if (e - d).days <= t]
        after = [e for e in cands if (e - d).days >= t]
        if before:
            picked.add(before[-1])
        if after:
            picked.add(after[0])
    chosen = set(sorted(picked)[: cfg.max_expiries])
    for c in catalyst_dates:
        after = [e for e in cands if e > c]
        if c >= d and after:
            chosen.add(after[0])
    return sorted(chosen)


def passes(c: Contract, cfg: OptionsConfig) -> bool:
    """Quality gate for a contract: open interest, a two-sided quote within the spread limit, IV."""
    if c.iv is None or c.iv <= 0 or c.bid is None or c.ask is None or c.bid <= 0 or c.ask < c.bid:
        return False
    mid = (c.bid + c.ask) / 2
    return c.open_interest >= cfg.min_open_interest and (c.ask - c.bid) / mid <= cfg.max_spread_pct


def _interp(points: list[tuple[int, float]], t: int) -> tuple[float | None, str | None]:
    """ATM IV at t days from (days, iv) points, linear in total variance."""
    if not points:
        return None, "no expiry with a usable ATM IV"
    for d, iv in points:
        if d == t:
            return iv, None
    lo = [(d, iv) for d, iv in points if d < t]
    hi = [(d, iv) for d, iv in points if d > t]
    if not lo:
        return None, f"before the first usable expiry ({points[0][0]} days)"
    if not hi:
        return None, f"beyond the last usable expiry ({points[-1][0]} days); not extrapolated"
    (d1, v1), (d2, v2) = lo[-1], hi[0]
    w1, w2 = v1 * v1 * d1, v2 * v2 * d2
    var = (w1 * (d2 - t) + w2 * (t - d1)) / (d2 - d1)
    return math.sqrt(var / t), None


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, DP) if den > 0 else None


def compute(
    chain: Chain,
    d: date,
    cfg: OptionsConfig,
    catalysts: Sequence[CatalystDate] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    reasons: dict[str, str] = {}
    per_expiry: list[dict[str, Any]] = []
    points: list[tuple[int, float]] = []
    vol = {"call": 0, "put": 0}
    oi = {"call": 0, "put": 0}
    considered = [e for e in chain.expiries if 0 < (e.expiry - d).days <= cfg.max_days]
    for e in considered:
        dte = (e.expiry - d).days
        vol["call"] += sum(c.volume for c in e.calls)
        vol["put"] += sum(c.volume for c in e.puts)
        oi["call"] += sum(c.open_interest for c in e.calls)
        oi["put"] += sum(c.open_interest for c in e.puts)
        row: dict[str, Any] = {
            "expiry": e.expiry.isoformat(),
            "dte": dte,
            "atm_strike": None,
            "atm_iv": None,
        }
        if chain.spot is not None:
            puts = {p.strike: p for p in e.puts}
            both = [c for c in e.calls if c.strike in puts]
            if both:
                atm = min(both, key=lambda c: (abs(c.strike - chain.spot), c.strike))  # type: ignore[operator]
                usable = [x.iv for x in (atm, puts[atm.strike]) if passes(x, cfg)]
                row["atm_strike"] = atm.strike
                if usable:
                    iv = sum(usable) / len(usable)  # type: ignore[arg-type]
                    row["atm_iv"] = round(iv, DP)
                    points.append((dte, iv))
                else:
                    row["reason"] = "ATM contracts fail the quality gates"
            else:
                row["reason"] = "no strike listed as both call and put"
        per_expiry.append(row)

    thin = not points
    term: dict[str, float | None] = {}
    for t in cfg.term_days:
        why: str | None
        if chain.spot is None:
            term[str(t)], why = None, "no underlying price"
        elif thin:
            term[str(t)], why = (
                None,
                (
                    f"thin chain: no ATM contract passed the gates (open interest ≥ "
                    f"{cfg.min_open_interest}, spread ≤ {cfg.max_spread_pct:.0%})"
                ),
            )
        else:
            v, why = _interp(points, t)
            term[str(t)] = None if v is None else round(v, DP)
        if why:
            reasons[f"atm_iv_{t}"] = why
    if not considered:
        reasons["chain"] = f"no listed expiry within {cfg.max_days} days"

    from aether.options.analytics import implied_moves, skew_30

    # M8 analytics: 30-day 25-delta skew and implied moves into catalysts. A thin chain reports
    # neither (the ATM gates already failed), only the flag.
    skew, skew_why = skew_30(chain, d, cfg)
    if thin:
        skew = {"value": None, "expiries": skew["expiries"]}
        skew_why = "thin chain"
    if skew_why:
        reasons["skew_30"] = skew_why
    moves = implied_moves(chain, d, catalysts, cfg)
    if thin:
        moves = [
            {**m, "strike": None, "straddle": None, "move": None, "reason": "thin chain"}
            for m in moves
        ]

    metrics = {
        "spot": chain.spot,
        "skew_30": skew["value"],
        "skew_expiries": skew["expiries"],
        "implied_moves": moves,
        "atm_iv_30": term.get("30"),
        "term": term,
        "expiries": per_expiry,
        "put_call_volume": _ratio(vol["put"], vol["call"]),
        "put_call_oi": _ratio(oi["put"], oi["call"]),
        "total_volume": vol["call"] + vol["put"],
        "total_open_interest": oi["call"] + oi["put"],
    }
    quality = {
        "thin": thin,
        "expiries_considered": len(considered),
        "reasons": reasons,
        "gates": {
            "min_open_interest": cfg.min_open_interest,
            "max_spread_pct": cfg.max_spread_pct,
        },
    }
    return metrics, quality
