"""Stance hysteresis and cooldown (spec §6.2), enforced in code; the model's
`stance_change_justification` is only a claim that code checks. Pure functions.

A proposed stance that differs from the current one is accepted only if

1. **material event:** the justification cites a non-quarantined event of post-cap materiality
   >= `trigger_min_materiality`, published after the previous conclusion was made, **or**
2. **score threshold:** each of the last `consecutive_days` daily scorecard totals lies beyond the
   current stance's band by at least `threshold_margin`, in the direction of the change
   (bands: AVOID < avoid_below <= TRIM < trim_below <= HOLD < accumulate_from <= ACCUMULATE),

**and** at least `cooldown_days` have passed since the last stance change. A cited T1 RISK event
that qualifies under (1) bypasses the cooldown, on a downgrade only.

Otherwise the previous stance is kept; the run is stored as a "held" update with the reason.
The theme tilt has no scorecard: only rule (1) and the cooldown apply to it.
The first conclusion for a ticker (or the theme) is accepted as proposed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from aether.config import ConclusionParams

ORDER = {"AVOID": 0, "TRIM": 1, "HOLD": 2, "ACCUMULATE": 3}
TILT_ORDER = {"QTUM": 0, "NEUTRAL": 1, "PURE_PLAYS": 2}


@dataclass(frozen=True)
class Previous:
    id: int
    stance: str
    created_at: str  # UTC ISO, when the previous conclusion was made
    last_change: str  # as_of (date) of the last accepted stance change


@dataclass(frozen=True)
class CitedEvent:
    id: int
    cls: str
    materiality: int
    trust_tier: str
    published_at: str
    quarantined: bool


@dataclass(frozen=True)
class Decision:
    stance: str
    held: bool
    reason: str | None  # why a flip was held, or which trigger accepted it
    trigger: str | None  # 'material_event' | 'score_threshold' | None


def band_of(total: float, p: ConclusionParams) -> str:
    if total < p.avoid_below:
        return "AVOID"
    if total < p.trim_below:
        return "TRIM"
    if total < p.accumulate_from:
        return "HOLD"
    return "ACCUMULATE"


def band_bounds(stance: str, p: ConclusionParams) -> tuple[float, float]:
    return {
        "AVOID": (-math.inf, p.avoid_below),
        "TRIM": (p.avoid_below, p.trim_below),
        "HOLD": (p.trim_below, p.accumulate_from),
        "ACCUMULATE": (p.accumulate_from, math.inf),
    }[stance]


def score_trigger(
    current: str, proposed: str, totals: Sequence[float | None], p: ConclusionParams
) -> bool:
    """`totals`: daily scorecard totals, oldest first, ending at the run date."""
    last = list(totals[-p.consecutive_days :])
    if len(last) < p.consecutive_days or any(t is None for t in last):
        return False
    lo, hi = band_bounds(current, p)
    if ORDER[proposed] > ORDER[current]:
        return math.isfinite(hi) and all(
            t is not None and t >= hi + p.threshold_margin for t in last
        )
    return math.isfinite(lo) and all(t is not None and t <= lo - p.threshold_margin for t in last)


def qualifying_events(
    cited: Sequence[int], events: Mapping[int, CitedEvent], since: str, p: ConclusionParams
) -> list[CitedEvent]:
    out = []
    for eid in cited:
        e = events.get(eid)
        if (
            e is not None
            and not e.quarantined
            and e.materiality >= p.trigger_min_materiality
            and e.published_at > since
        ):
            out.append(e)
    return out


def decide(
    *,
    kind: str,
    proposed: str,
    previous: Previous | None,
    cited_event_ids: Sequence[int],
    events: Mapping[int, CitedEvent],
    totals: Sequence[float | None],
    as_of: date,
    p: ConclusionParams,
) -> Decision:
    if previous is None:
        return Decision(proposed, False, "first conclusion", None)
    current = previous.stance
    if proposed == current:
        return Decision(current, False, None, None)

    qual = qualifying_events(cited_event_ids, events, previous.created_at, p)
    trigger = None
    if qual:
        trigger = "material_event"
    elif kind == "ticker" and score_trigger(current, proposed, totals, p):
        trigger = "score_threshold"
    if trigger is None:
        why = (
            f"no qualifying trigger: needs a cited event of materiality ≥ "
            f"{p.trigger_min_materiality} since the last conclusion"
        )
        if kind == "ticker":
            why += (
                f", or the score beyond the {current} band by {p.threshold_margin:g} points for "
                f"{p.consecutive_days} days"
            )
        return Decision(current, True, why, None)

    until = date.fromisoformat(previous.last_change) + timedelta(days=p.cooldown_days)
    if as_of < until:
        downgrade = kind == "ticker" and ORDER[proposed] < ORDER[current]
        bypass = downgrade and any(e.trust_tier == "T1" and e.cls == "RISK" for e in qual)
        if not bypass:
            return Decision(current, True, f"cooldown until {until.isoformat()}", trigger)
        refs = ", ".join(f"#{e.id}" for e in qual if e.trust_tier == "T1" and e.cls == "RISK")
        return Decision(proposed, False, f"T1 RISK event {refs} bypassed the cooldown", trigger)
    if trigger == "material_event":
        return Decision(
            proposed, False, "material event " + ", ".join(f"#{e.id}" for e in qual), trigger
        )
    return Decision(proposed, False, "score threshold crossed", trigger)
