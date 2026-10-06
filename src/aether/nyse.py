"""NYSE session calendar (spec §6.3). The only module that imports `exchange_calendars`.

Sessions are `date`s; closes are UTC datetimes and include early closes (13:00 ET half-days).
The calendar spans 2020-01-01 to the end of the year after next, which covers the stored price
history and any event anchor the reaction engine can see.
"""

from __future__ import annotations

import bisect
from datetime import UTC, date, datetime
from functools import lru_cache

START = date(2020, 1, 1)


@lru_cache(maxsize=4)
def _calendar(end_year: int) -> tuple[tuple[date, ...], tuple[datetime, ...]]:
    import exchange_calendars as xc

    cal = xc.get_calendar("XNYS", start=START.isoformat(), end=f"{end_year}-12-31")
    sessions = tuple(ts.date() for ts in cal.sessions)
    closes = tuple(ts.to_pydatetime().astimezone(UTC) for ts in cal.closes)
    return sessions, closes


def _cal() -> tuple[tuple[date, ...], tuple[datetime, ...]]:
    return _calendar(datetime.now(UTC).year + 2)


def sessions(start: date, end: date) -> list[date]:
    """Sessions in [start, end]."""
    s, _ = _cal()
    return list(s[bisect.bisect_left(s, start) : bisect.bisect_right(s, end)])


def is_session(d: date) -> bool:
    s, _ = _cal()
    i = bisect.bisect_left(s, d)
    return i < len(s) and s[i] == d


def close_utc(session: date) -> datetime:
    s, c = _cal()
    i = bisect.bisect_left(s, session)
    if i >= len(s) or s[i] != session:
        raise ValueError(f"{session} is not an NYSE session")
    return c[i]


def first_session_closing_after(ts: datetime) -> date | None:
    """The first session whose close is strictly after `ts` (spec §6.3 anchor day t0)."""
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    s, c = _cal()
    i = bisect.bisect_right(c, ts.astimezone(UTC))
    return s[i] if i < len(s) else None


def offset(session: date, n: int) -> date | None:
    """The session `n` sessions after (n > 0) or before (n < 0) `session`."""
    s, _ = _cal()
    i = bisect.bisect_left(s, session)
    if i >= len(s) or s[i] != session:
        raise ValueError(f"{session} is not an NYSE session")
    j = i + n
    return s[j] if 0 <= j < len(s) else None
