"""Total-return series from split-adjusted closes + cash dividends (spec §6.5).

`TR_t = TR_{t-1} * (c_t + div_t) / c_{t-1}`, where `div_t` is the cash dividend per share going
ex on session t. A dividend whose ex-date is not a session of that symbol applies on the next
session; one dated after the last session is not applied yet, and one on or before the first
session can't be applied (there's no prior close to reinvest from).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

Closes = Sequence[tuple[str, float]]  # [(YYYY-MM-DD, close)], ascending


def total_return_levels(closes: Closes, dividends: Mapping[str, float]) -> list[tuple[str, float]]:
    """TR level per own session, starting at 1.0 on the first close."""
    if not closes:
        return []
    days = [d for d, _ in closes]
    div_on = [0.0] * len(days)
    for ex, amount in dividends.items():
        i = bisect.bisect_left(days, ex)
        if 0 < i < len(days):
            div_on[i] += amount
    out = [(days[0], 1.0)]
    level = 1.0
    for i in range(1, len(days)):
        prev, cur = closes[i - 1][1], closes[i][1]
        level *= (cur + div_on[i]) / prev
        out.append((days[i], level))
    return out


def align(levels: Sequence[tuple[str, float]], calendar: Sequence[str]) -> NDArray[np.float64]:
    """Levels on `calendar`: NaN before the symbol's first session, forward-filled after it
    (a missing bar then shows up as a 0 return that day and the move lands on the next bar).
    Sessions the symbol has but the calendar lacks are dropped; levels compound, so no return is
    lost."""
    out = np.full(len(calendar), np.nan)
    if not levels:
        return out
    j, last = 0, math.nan
    for i, d in enumerate(calendar):
        while j < len(levels) and levels[j][0] <= d:
            last = levels[j][1]
            j += 1
        out[i] = last
    return out


def simple_returns(levels: NDArray[np.float64]) -> NDArray[np.float64]:
    """Row-wise simple returns of a (T,) or (T, N) level array; row 0 and pre-listing are NaN."""
    r = np.full(levels.shape, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        r[1:] = levels[1:] / levels[:-1] - 1.0
    return r
