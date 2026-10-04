"""Daily OHLCV providers: yfinance (primary) and Massive, formerly Polygon (fallback).

Both serve **split-adjusted, not dividend-adjusted** prices (yfinance `auto_adjust=False`
"Open/High/Low/Close", Massive `adjusted=true`), so a failover never mixes conventions.

`fetch_daily` raises `ProviderError` on transport, HTTP or rate-limit failures. An empty list
means the provider has no bars for that symbol/range; that is not a failure.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

US_EASTERN = ZoneInfo("America/New_York")
PRICE_DECIMALS = 6


@dataclass(frozen=True)
class Bar:
    d: date
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    volume: int
    provider: str


class ProviderError(Exception):
    """A provider failed (network, HTTP status, rate limit, malformed payload)."""


class PriceProvider(Protocol):
    @property
    def name(self) -> str: ...

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
        """Daily bars for `start..end` inclusive, ascending."""
        ...


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


# --------------------------------------------------------------------------- yfinance


HistoryFn = Callable[[str, date, date], Any]


def _yf_history(symbol: str, start: date, end: date) -> Any:
    import yfinance as yf

    # The container root FS is read-only; keep yfinance's tz cache on the /tmp tmpfs.
    yf.set_tz_cache_location("/tmp/yfinance")  # noqa: S108
    return yf.Ticker(symbol).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),  # yfinance's `end` is exclusive
        interval="1d",
        auto_adjust=False,
        actions=False,
        raise_errors=True,
    )


def bars_from_frame(frame: Any, provider: str = "yfinance") -> list[Bar]:
    """Convert a yfinance history DataFrame (DatetimeIndex, Open/High/Low/Close/Volume)."""
    if frame is None or len(frame) == 0:
        return []
    bars: list[Bar] = []
    cols = (frame["Open"], frame["High"], frame["Low"], frame["Close"], frame["Volume"])
    for ts, o, h, lo, c, v in zip(frame.index, *cols, strict=True):
        o, h, lo, c, v = float(o), float(h), float(lo), float(c), float(v)
        if not _finite(o, h, lo, c, v):
            continue  # yfinance emits NaN rows for non-trading/partial days
        # yfinance prices carry float32 noise (45.15999984741211); 6 dp is far below a tick.
        o, h, lo, c = (round(x, PRICE_DECIMALS) for x in (o, h, lo, c))
        bars.append(Bar(ts.date(), o, h, lo, c, round(v), provider))
    return bars


@dataclass
class YFinanceProvider:
    history_fn: HistoryFn = _yf_history
    name: str = "yfinance"

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
        try:
            frame = self.history_fn(symbol, start, end)
            return bars_from_frame(frame, self.name)
        except Exception as exc:
            # yfinance raises a zoo of exception types (rate limit, JSON, curl). Collapse them.
            raise ProviderError(f"yfinance {symbol}: {type(exc).__name__}: {exc}"[:300]) from exc


# --------------------------------------------------------------------------- Massive


MASSIVE_BASE = "https://api.massive.com"
MASSIVE_HOST = "api.massive.com"
# Free "Stocks Basic" tier: 5 calls/minute.
MASSIVE_MIN_INTERVAL_S = 12.5


@dataclass
class MassiveProvider:
    """Massive aggregates API. The key goes in the Authorization header, never the URL."""

    api_key: str
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30))
    min_interval_s: float = MASSIVE_MIN_INTERVAL_S
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    name: str = "massive"
    _last_call: float | None = field(default=None, init=False, repr=False)

    def _throttle(self) -> None:
        if self._last_call is not None:
            wait = self.min_interval_s - (self.clock() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = self.clock()

    def _get(self, url: str, params: dict[str, str] | None) -> dict[str, Any]:
        if urlsplit(url).hostname != MASSIVE_HOST:
            # Never send the API key to a host taken from a response body.
            raise ProviderError(f"massive: refusing to follow URL on another host: {url[:80]}")
        self._throttle()
        try:
            resp = self.client.get(
                url, params=params, headers={"Authorization": f"Bearer {self.api_key}"}
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"massive: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"massive: HTTP {resp.status_code}")
        try:
            body: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise ProviderError("massive: invalid JSON") from exc
        if body.get("status") not in ("OK", "DELAYED"):
            raise ProviderError(f"massive: status {str(body.get('status'))[:40]}")
        return body

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
        url: str | None = (
            f"{MASSIVE_BASE}/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{start.isoformat()}/{end.isoformat()}"
        )
        params: dict[str, str] | None = {"adjusted": "true", "sort": "asc", "limit": "50000"}
        bars: list[Bar] = []
        while url:
            body = self._get(url, params)
            for r in body.get("results") or []:
                try:
                    d = datetime.fromtimestamp(int(r["t"]) / 1000, US_EASTERN).date()
                    o, h, lo, c = float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])
                    v = float(r["v"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ProviderError("massive: malformed bar") from exc
                if _finite(o, h, lo, c, v):
                    bars.append(Bar(d, o, h, lo, c, round(v), self.name))
            url, params = body.get("next_url"), None  # next_url carries its own cursor
        return bars


# --------------------------------------------------------------------------- failover


@dataclass
class FailoverPriceProvider:
    """Primary with automatic failover (spec §4).

    - A primary `ProviderError` (or an empty result) is retried on the fallback for that symbol.
    - After `threshold` consecutive primary failures the primary is skipped entirely, and probed
      again once `probe_after` has passed. State is in-memory: a worker restart is a probe too.
    """

    primary: PriceProvider
    fallback: PriceProvider | None
    threshold: int = 3
    probe_after: timedelta = timedelta(hours=24)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    consecutive_failures: int = field(default=0, init=False)
    tripped_at: datetime | None = field(default=None, init=False)

    @property
    def name(self) -> str:
        return self.primary.name if self.fallback is None else f"{self.primary.name}+failover"

    def _primary_enabled(self) -> bool:
        if self.fallback is None or self.tripped_at is None:
            return True
        return self.now() - self.tripped_at >= self.probe_after

    def _record_primary_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold and self.fallback is not None:
            if self.tripped_at is None:
                log.warning(
                    "%s failed %d times in a row; switching to %s",
                    self.primary.name,
                    self.consecutive_failures,
                    self.fallback.name,
                )
            self.tripped_at = self.now()  # (re)start the probe timer

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
        errors: list[str] = []
        if self._primary_enabled():
            try:
                bars = self.primary.fetch_daily(symbol, start, end)
            except ProviderError as exc:
                errors.append(str(exc))
                self._record_primary_failure()
            else:
                if self.tripped_at is not None:
                    log.info("%s recovered; switching back", self.primary.name)
                self.consecutive_failures, self.tripped_at = 0, None
                if bars or self.fallback is None:
                    return bars
        if self.fallback is not None:
            try:
                return self.fallback.fetch_daily(symbol, start, end)
            except ProviderError as exc:
                errors.append(str(exc))
        if errors:
            raise ProviderError("; ".join(errors))
        return []
