"""USD/SGD reference rate (spec §1.4, §4): reporting only, never used in targets or trades.

Primary: yfinance `SGD=X` daily closes (SGD per 1 USD; personal use, like prices). Fallback:
the ECB's official euro reference rates (no key), crossed as SGD/USD = (SGD per EUR) / (USD per
EUR). ECB rates exist only on ECB business days.

`fetch` raises `ProviderError` on failure; an empty list is not a failure.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

import httpx

from aether.providers.prices import HistoryFn, ProviderError, _yf_history, bars_from_frame

PAIR = "USDSGD"
YF_SYMBOL = "SGD=X"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_HOST = "www.ecb.europa.eu"
RATE_DP = 6


@dataclass(frozen=True)
class FxRate:
    d: date
    rate: float  # SGD per 1 USD
    provider: str


class FxProvider(Protocol):
    @property
    def name(self) -> str: ...

    def fetch(self, start: date, end: date) -> list[FxRate]: ...


@dataclass
class YFinanceFx:
    history_fn: HistoryFn = _yf_history
    name: str = "yfinance"

    def fetch(self, start: date, end: date) -> list[FxRate]:
        try:
            bars = bars_from_frame(self.history_fn(YF_SYMBOL, start, end), self.name)
        except Exception as exc:
            raise ProviderError(f"yfinance {YF_SYMBOL}: {type(exc).__name__}: {exc}"[:300]) from exc
        return [FxRate(b.d, round(b.c, RATE_DP), self.name) for b in bars if b.c > 0]


def parse_ecb(xml_text: str) -> list[FxRate]:
    if re.search(r"<!DOCTYPE|<!ENTITY", xml_text[:4000], re.IGNORECASE):
        raise ProviderError("ecb: DTD/entity declarations are not accepted")
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - DTDs rejected above
    except ET.ParseError as exc:
        raise ProviderError(f"ecb: malformed XML: {exc}") from exc
    out: list[FxRate] = []
    for cube in root.iter():
        t = cube.attrib.get("time")
        if not t:
            continue
        rates = {c.attrib.get("currency"): c.attrib.get("rate") for c in cube}
        usd, sgd = rates.get("USD"), rates.get("SGD")
        if not usd or not sgd:
            continue
        try:
            rate = float(sgd) / float(usd)
            d = date.fromisoformat(t)
        except ValueError:
            continue
        if rate > 0:
            out.append(FxRate(d, round(rate, RATE_DP), "ecb"))
    return sorted(out, key=lambda r: r.d)


@dataclass
class EcbFx:
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30))
    name: str = "ecb"

    def fetch(self, start: date, end: date) -> list[FxRate]:
        try:
            r = self.client.get(ECB_URL)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ecb: {type(exc).__name__}") from exc
        if r.status_code != 200 or r.url.host != ECB_HOST:
            raise ProviderError(f"ecb: HTTP {r.status_code}")
        return [x for x in parse_ecb(r.text) if start <= x.d <= end]


@dataclass
class FallbackFx:
    primary: FxProvider
    fallback: FxProvider | None = None
    name: str = "fx"

    def fetch(self, start: date, end: date) -> list[FxRate]:
        try:
            rows = self.primary.fetch(start, end)
            if rows:
                return rows
        except ProviderError:
            if self.fallback is None:
                raise
        if self.fallback is None:
            return []
        return self.fallback.fetch(start, end)


def default_window(today: date, days: int = 30) -> tuple[date, date]:
    return today - timedelta(days=days), today
