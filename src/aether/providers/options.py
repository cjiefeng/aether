"""Listed option chains from yfinance (spec §4, §6.8; M5 snapshot). Research only.

`fetch_raw` returns a plain JSON-able dict (spot + chains for the nearest expiries), so tests
replay a dict recorded once from a real chain. Tiger option chains are not used: API option
quotes need a paid market-data permission (checked 2026-10-04; unchanged in M8).

Raises `ProviderError` on failure.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from aether.providers.prices import ProviderError

CONTRACT_FIELDS = (
    "strike",
    "bid",
    "ask",
    "lastPrice",
    "volume",
    "openInterest",
    "impliedVolatility",
)


@dataclass(frozen=True)
class Contract:
    strike: float
    bid: float | None
    ask: float | None
    volume: int
    open_interest: int
    iv: float | None


@dataclass(frozen=True)
class Expiry:
    expiry: date
    calls: tuple[Contract, ...]
    puts: tuple[Contract, ...]


@dataclass(frozen=True)
class Chain:
    symbol: str
    spot: float | None
    expiries: tuple[Expiry, ...]  # the expiries fetched
    # M8: every listed expiry (so "the first expiry after a catalyst" is never a fetched-only
    # guess). Empty in chains recorded before M8: then the fetched expiries stand in.
    listed: tuple[date, ...] = ()


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _contracts(rows: list[dict[str, Any]]) -> tuple[Contract, ...]:
    out = []
    for r in rows:
        strike = _num(r.get("strike"))
        if strike is None or strike <= 0:
            continue
        out.append(
            Contract(
                strike=strike,
                bid=_num(r.get("bid")),
                ask=_num(r.get("ask")),
                volume=int(_num(r.get("volume")) or 0),
                open_interest=int(_num(r.get("openInterest")) or 0),
                iv=_num(r.get("impliedVolatility")),
            )
        )
    return tuple(sorted(out, key=lambda c: c.strike))


def parse_chain(raw: dict[str, Any]) -> Chain:
    try:
        return Chain(
            symbol=str(raw["symbol"]),
            spot=_num(raw.get("spot")),
            expiries=tuple(
                Expiry(
                    date.fromisoformat(e["expiry"]), _contracts(e["calls"]), _contracts(e["puts"])
                )
                for e in raw["expiries"]
            ),
            listed=tuple(sorted(date.fromisoformat(x) for x in raw.get("listed", []))),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError(f"options: malformed chain: {type(exc).__name__}") from exc


def _records(frame: Any) -> list[dict[str, Any]]:
    cols = [c for c in CONTRACT_FIELDS if c in frame.columns]
    return [
        {c: (None if v != v else v) for c, v in zip(cols, row, strict=True)}
        for row in frame[cols].itertuples(index=False)
    ]


Chooser = Callable[[list[date]], list[date]]


def _yf_fetch_raw(symbol: str, choose: Chooser) -> dict[str, Any]:
    import yfinance as yf

    yf.set_tz_cache_location("/tmp/yfinance")  # noqa: S108 - read-only root FS
    t = yf.Ticker(symbol)
    listed = sorted(date.fromisoformat(e) for e in t.options)
    expiries = [d.isoformat() for d in choose(listed)]
    out: dict[str, Any] = {
        "symbol": symbol,
        "spot": None,
        "expiries": [],
        "listed": [d.isoformat() for d in listed],
    }
    for e in expiries:
        oc = t.option_chain(e)
        if out["spot"] is None:
            out["spot"] = (getattr(oc, "underlying", None) or {}).get("regularMarketPrice")
        out["expiries"].append(
            {"expiry": e, "calls": _records(oc.calls), "puts": _records(oc.puts)}
        )
    return out


FetchRaw = Callable[[str, Chooser], dict[str, Any]]


@dataclass
class YFinanceOptions:
    fetch_raw_fn: FetchRaw = _yf_fetch_raw
    name: str = "yfinance"

    def fetch(self, symbol: str, choose: Chooser) -> Chain:
        """`choose` picks which listed expiries to download (see options.snapshot)."""
        try:
            raw = self.fetch_raw_fn(symbol, choose)
        except Exception as exc:
            raise ProviderError(f"yfinance options {symbol}: {type(exc).__name__}"[:300]) from exc
        return parse_chain(raw)
