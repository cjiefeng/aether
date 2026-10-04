"""Synthetic price panels for the M4 portfolio tests. Every number is generated (seeded RNG);
symbols other than the real benchmarks the code keys on (QTUM/QQQ/SOXX) are made up."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import Engine

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import dividends, prices_daily
from aether.db.types import utcnow_iso
from tests.conftest import seed_tickers

SLEEVE = ("ACME", "DEMO", "EXMP", "FAKE")
UNIVERSE = [
    ("QTUM", "etf"),
    *((s, "pure_play") for s in SLEEVE),
    ("QQQ", "benchmark"),
    ("SOXX", "benchmark"),
]


def weekdays(n: int, start: date = date(2025, 1, 2)) -> list[str]:
    out: list[str] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def random_closes(
    n: int, seed: int, drift: float = 0.0005, vol: float = 0.02, start: float = 50.0
) -> list[float]:
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n - 1)
    return [round(float(x), 6) for x in start * np.concatenate(([1.0], np.cumprod(1.0 + r)))]


def panel(n: int = 260, late: dict[str, int] | None = None) -> dict[str, list[tuple[str, float]]]:
    """Closes per symbol over n weekdays. `late` maps a symbol to the session it starts on."""
    days = weekdays(n)
    late = late or {}
    out: dict[str, list[tuple[str, float]]] = {}
    for i, (sym, _) in enumerate(UNIVERSE):
        vol = 0.012 if sym in ("QTUM", "QQQ", "SOXX") else 0.03 + 0.01 * i
        closes = random_closes(n, seed=100 + i, drift=0.0004 * (i + 1), vol=vol)
        first = late.get(sym, 0)
        out[sym] = list(zip(days[first:], closes[first:], strict=True))
    return out


def seed_panel(
    engine: Engine,
    closes: dict[str, list[tuple[str, float]]],
    divs: dict[str, dict[str, Decimal]] | None = None,
) -> None:
    seed_tickers(engine, UNIVERSE)
    rows = [
        {
            "symbol": s,
            "d": d,
            "o": c,
            "h": c,
            "l": c,
            "c": c,
            "volume": 1000,
            "provider": "synthetic",
            "fetched_at": utcnow_iso(),
        }
        for s, series in closes.items()
        for d, c in series
    ]
    drows = [
        {
            "symbol": s,
            "ex_date": ex,
            "amount_micros": amt,
            "currency": "USD",
            "provider": "synthetic",
            "fetched_at": utcnow_iso(),
        }
        for s, m in (divs or {}).items()
        for ex, amt in m.items()
    ]
    with write_tx(engine) as conn:
        upsert(conn, prices_daily, rows, key_cols=["symbol", "d"])
        upsert(conn, dividends, drows, key_cols=["symbol", "ex_date"])
