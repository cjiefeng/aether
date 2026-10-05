"""FINRA bi-weekly equity short interest files (spec §4 "short interest"; M8).

`https://cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv`: pipe-delimited, one file per
settlement date, exchange-listed and OTC securities since June 2021 (verified 2026-10-05). A
missing date answers 403. FINRA's terms allow non-commercial use; Aether keeps only the rows for
its own symbols and never stores the file.

Settlement dates are mid-month and month-end; when the nominal date isn't a business day the file
carries the business day before it (2026-08-15 was a Saturday → `shrt20260814.csv`).

Raises `ProviderError` on transport errors and malformed files; `fetch` returns None when the
file for a date doesn't exist (yet).
"""

from __future__ import annotations

import csv
import io
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from aether.providers.prices import ProviderError

HOST = "cdn.finra.org"
URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{d}.csv"
MAX_BYTES = 20_000_000  # a full file is ~3 MB
MAX_STEP_BACK = 3
REQUIRED = (
    "symbolCode",
    "currentShortPositionQuantity",
    "previousShortPositionQuantity",
    "averageDailyVolumeQuantity",
    "daysToCoverQuantity",
    "settlementDate",
)


@dataclass(frozen=True)
class ShortRow:
    symbol: str
    settlement_date: date
    short_shares: int
    prev_short_shares: int | None
    avg_daily_volume: int | None
    days_to_cover: float | None


def file_url(d: date) -> str:
    return URL.format(d=d.strftime("%Y%m%d"))


def nominal_dates(today: date, months_back: int) -> list[date]:
    """Mid-month (15th) and month-end dates of this month and the `months_back` months before it,
    up to today, newest first."""
    out: list[date] = []
    y, m = today.year, today.month
    for _ in range(months_back + 1):
        out += [date(y, m, monthrange(y, m)[1]), date(y, m, 15)]
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return [d for d in out if d <= today]


def candidates(nominal: date) -> list[date]:
    """The nominal date, then up to MAX_STEP_BACK earlier weekdays (holidays are found by 403)."""
    out: list[date] = []
    d = nominal
    while len(out) <= MAX_STEP_BACK:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _int(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def parse(text: str, symbols: set[str]) -> list[ShortRow]:
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    if reader.fieldnames is None or any(c not in reader.fieldnames for c in REQUIRED):
        raise ProviderError("finra: unexpected columns")
    out = []
    for r in reader:
        sym = (r.get("symbolCode") or "").strip()
        if sym not in symbols:
            continue
        short = _int(r.get("currentShortPositionQuantity"))
        try:
            settled = date.fromisoformat((r.get("settlementDate") or "").strip())
        except ValueError:
            continue
        if short is None or short < 0:
            continue
        out.append(
            ShortRow(
                symbol=sym,
                settlement_date=settled,
                short_shares=short,
                prev_short_shares=_int(r.get("previousShortPositionQuantity")),
                avg_daily_volume=_int(r.get("averageDailyVolumeQuantity")),
                days_to_cover=_float(r.get("daysToCoverQuantity")),
            )
        )
    return out


@dataclass
class FinraShortInterest:
    client: httpx.Client = field(
        default_factory=lambda: httpx.Client(timeout=60, headers={"User-Agent": "aether/0.1"})
    )
    name: str = "finra"

    def fetch(self, d: date, symbols: set[str]) -> list[ShortRow] | None:
        url = file_url(d)
        try:
            with self.client.stream("GET", url) as r:
                if r.status_code in (403, 404):
                    return None
                if r.status_code != 200 or r.url.host != HOST:
                    raise ProviderError(f"finra: HTTP {r.status_code} for {d}")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise ProviderError(f"finra: file for {d} exceeds {MAX_BYTES} bytes")
        except httpx.HTTPError as exc:
            raise ProviderError(f"finra: {type(exc).__name__}") from exc
        return parse(buf.decode("utf-8", errors="replace"), symbols)
