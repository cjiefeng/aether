"""Cash dividends per share: yfinance (primary) and Massive (fallback).

Both serve **split-adjusted cash per share** (Yahoo adjusts its dividend history for splits;
Massive's `split_adjusted_cash_amount`), matching the split-adjusted closes in `prices_daily`.
Total return is computed in code (`portfolio/total_return.py`), never taken from a provider's
adjusted close, so the providers' conventions can't mix.

An empty list is normal (most pure-plays pay nothing) and is not a failure, so unlike prices
the fallback is tried only on `ProviderError`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from aether.providers.prices import (
    MASSIVE_BASE,
    MASSIVE_MIN_INTERVAL_S,
    PRICE_DECIMALS,
    MassiveProvider,
    ProviderError,
)

log = logging.getLogger(__name__)

# Two years of dividends fit on one page; a cursor that never ends is a provider fault.
MASSIVE_MAX_PAGES = 20


@dataclass(frozen=True)
class Dividend:
    ex_date: date
    amount: Decimal  # USD per share, split-adjusted
    provider: str


class DividendProvider(Protocol):
    @property
    def name(self) -> str: ...

    def fetch_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        """Cash dividends with ex-date in `start..end` inclusive, ascending."""
        ...


def _amount(value: Any) -> Decimal | None:
    """Positive, finite amount rounded to 6 dp (drops float32 noise); None if unusable."""
    try:
        d = Decimal(str(round(float(value), PRICE_DECIMALS)))
    except (TypeError, ValueError, InvalidOperation):
        return None
    return d if d.is_finite() and d > 0 else None


# --------------------------------------------------------------------------- yfinance


DividendsFn = Callable[[str], Any]


def _yf_dividends(symbol: str) -> Any:
    import yfinance as yf

    yf.set_tz_cache_location("/tmp/yfinance")  # noqa: S108  (read-only root FS)
    return yf.Ticker(symbol).dividends


@dataclass
class YFinanceDividends:
    dividends_fn: DividendsFn = _yf_dividends
    name: str = "yfinance"

    def fetch_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        try:
            series = self.dividends_fn(symbol)
        except Exception as exc:
            raise ProviderError(
                f"yfinance dividends {symbol}: {type(exc).__name__}: {exc}"[:300]
            ) from exc
        out: list[Dividend] = []
        if series is None:
            return out
        for ts, value in zip(series.index, series.to_numpy(), strict=True):
            d = ts.date()
            amt = _amount(value)
            if amt is not None and start <= d <= end:
                out.append(Dividend(d, amt, self.name))
        return sorted(out, key=lambda x: x.ex_date)


# --------------------------------------------------------------------------- Massive


@dataclass
class MassiveDividends:
    """Massive `GET /stocks/v1/dividends` (Stocks Basic: 2 years of history). Shares the
    throttling, host pinning and Bearer auth of `MassiveProvider`."""

    api_key: str
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30))
    min_interval_s: float = MASSIVE_MIN_INTERVAL_S
    name: str = "massive"
    _http: MassiveProvider = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = MassiveProvider(
            self.api_key, client=self.client, min_interval_s=self.min_interval_s
        )

    def fetch_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        url: str | None = f"{MASSIVE_BASE}/stocks/v1/dividends"
        params: dict[str, str] | None = {
            "ticker": symbol,
            "ex_dividend_date.gte": start.isoformat(),
            "ex_dividend_date.lte": end.isoformat(),
            "sort": "ex_dividend_date.asc",
            "limit": "1000",
        }
        out: list[Dividend] = []
        pages = 0
        while url:
            pages += 1
            if pages > MASSIVE_MAX_PAGES:
                raise ProviderError(f"massive: more than {MASSIVE_MAX_PAGES} pages of dividends")
            body = self._http._get(url, params)
            for r in body.get("results") or []:
                try:
                    d = date.fromisoformat(str(r["ex_dividend_date"]))
                    currency = str(r.get("currency") or "USD")
                    raw = r.get("split_adjusted_cash_amount", r.get("cash_amount"))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ProviderError("massive: malformed dividend") from exc
                if currency != "USD":
                    log.warning("massive dividend %s %s in %s skipped", symbol, d, currency)
                    continue
                amt = _amount(raw)
                if amt is not None:
                    out.append(Dividend(d, amt, self.name))
            url, params = body.get("next_url"), None
        return sorted(out, key=lambda x: x.ex_date)


# --------------------------------------------------------------------------- fallback


@dataclass
class FallbackDividends:
    primary: DividendProvider
    fallback: DividendProvider | None

    @property
    def name(self) -> str:
        return self.primary.name if self.fallback is None else f"{self.primary.name}+fallback"

    def fetch_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        try:
            return self.primary.fetch_dividends(symbol, start, end)
        except ProviderError as exc:
            if self.fallback is None:
                raise
            log.warning("dividends %s: %s; trying %s", symbol, exc, self.fallback.name)
            try:
                return self.fallback.fetch_dividends(symbol, start, end)
            except ProviderError as exc2:
                raise ProviderError(f"{exc}; {exc2}") from exc2
