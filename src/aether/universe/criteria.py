"""Deterministic eligibility (spec §6.7 criteria 1, 3, 4), computed in code. No LLM.

- **c1 listing:** a NYSE/Nasdaq listing for the CIK in SEC's `company_tickers_exchange.json`.
- **c3 size and liquidity:** market cap = latest close x shares outstanding (the latest
  `dei:EntityCommonStockSharesOutstanding` cover-page figures in XBRL companyfacts, summed across
  share classes; else the balance-sheet `us-gaap:CommonStockSharesOutstanding`; else the price
  provider's count, which may cover one class only, so it can only understate), at least
  `min_market_cap_usd`; the median close x volume over the last `liquidity_sessions` sessions,
  at least `min_median_dollar_volume_usd`. Unknown → fail.
- **c4 history:** at least `min_sessions` daily bars.

Prices come from the price provider for this check only (candidates aren't on the watchlist, so
nothing is stored in `prices_daily`). Money is `Decimal`; the stored JSON carries strings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from aether.config import UniverseConfig
from aether.providers.prices import Bar

PRICE_STALE_DAYS = 10  # the latest bar must be this recent for a market cap


MIN_PLAUSIBLE_SHARES = 1_000  # pre-IPO filings report placeholder counts such as 1 share
SHARE_CONCEPTS = (
    ("dei", "EntityCommonStockSharesOutstanding", "xbrl_dei"),
    ("us-gaap", "CommonStockSharesOutstanding", "xbrl_balance_sheet"),
)


@dataclass(frozen=True)
class Shares:
    shares: int
    as_of: str  # the cover-page / balance-sheet date, or the fetch date for the provider
    accession: str
    source: str = "xbrl_dei"  # xbrl_dei | xbrl_balance_sheet | yfinance


def shares_outstanding(companyfacts: Mapping[str, Any]) -> Shares | None:
    """The latest filing's cover-page share counts (summed over share classes), else the latest
    balance-sheet count. Implausibly small counts are ignored."""
    facts = companyfacts.get("facts") or {}
    for tax, concept, source in SHARE_CONCEPTS:
        units = ((facts.get(tax) or {}).get(concept) or {}).get("units", {}).get("shares")
        if not isinstance(units, list):
            continue
        rows = [
            u
            for u in units
            if isinstance(u, Mapping)
            and isinstance(u.get("val"), int | float)
            and u.get("val", 0) >= MIN_PLAUSIBLE_SHARES
            and isinstance(u.get("end"), str)
            and isinstance(u.get("accn"), str)
        ]
        if not rows:
            continue
        latest = max(rows, key=lambda u: (str(u.get("filed") or ""), u["end"], u["accn"]))
        accn, end = latest["accn"], latest["end"]
        # One filing reports one value per class (all with the same date).
        vals = {int(u["val"]) for u in rows if u["accn"] == accn and u["end"] == end}
        if source != "xbrl_dei":
            vals = {max(vals)}  # balance-sheet values aren't split by class
        return Shares(sum(vals), end, accn, source)
    return None


@dataclass(frozen=True)
class Criteria:
    c1: bool
    c3: bool
    c4: bool
    detail: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"c1": self.c1, "c3": self.c3, "c4": self.c4, **self.detail}


def evaluate(
    cfg: UniverseConfig,
    *,
    exchange: str | None,
    ticker: str | None,
    cik: str | None,
    bars: Sequence[Bar] | None,
    shares: Shares | None,
    today: date,
    price_error: str | None = None,
) -> Criteria:
    detail: dict[str, Any] = {"exchange": exchange, "ticker": ticker, "cik": cik}
    c1 = bool(exchange in cfg.exchanges and cik)
    reasons: list[str] = []
    if not c1:
        reasons.append("not listed on NYSE/Nasdaq (SEC exchange map)")

    bars = sorted(bars or [], key=lambda b: b.d)
    detail["sessions"] = len(bars)
    detail["price_provider"] = bars[-1].provider if bars else None
    if price_error:
        detail["price_error"] = price_error[:200]
    c4 = len(bars) >= cfg.min_sessions
    if not c4:
        reasons.append(f"history {len(bars)} < {cfg.min_sessions} sessions")

    mcap: Decimal | None = None
    mdv: Decimal | None = None
    if bars:
        last = bars[-1]
        detail["close"] = f"{Decimal(str(last.c)):.4f}"
        detail["close_date"] = last.d.isoformat()
        window = bars[-cfg.liquidity_sessions :]
        mdv = Decimal(str(median(b.c * b.volume for b in window))).quantize(Decimal(1))
        detail["median_dollar_volume"] = str(mdv)
        detail["liquidity_sessions"] = len(window)
        if shares is not None and last.d >= today - timedelta(days=PRICE_STALE_DAYS):
            mcap = (Decimal(str(last.c)) * shares.shares).quantize(Decimal(1))
    if shares is not None:
        detail["shares"] = shares.shares
        detail["shares_as_of"] = shares.as_of
        detail["shares_accession"] = shares.accession
        detail["shares_source"] = shares.source
    detail["market_cap"] = None if mcap is None else str(mcap)
    # Unknown isn't a measured failure: it never counts toward the consecutive-failure removal.
    detail["c3_unknown"] = mcap is None or mdv is None

    c3 = True
    if mcap is None:
        c3 = False
        reasons.append("market cap unknown (no recent price or no XBRL shares outstanding)")
    elif mcap < cfg.min_market_cap_usd:
        c3 = False
        reasons.append(f"market cap ${mcap:,} < ${cfg.min_market_cap_usd:,}")
    if mdv is None:
        c3 = False
        reasons.append("no volume data")
    elif mdv < cfg.min_median_dollar_volume_usd:
        c3 = False
        reasons.append(
            f"{cfg.liquidity_sessions}-session median dollar volume ${mdv:,} < "
            f"${cfg.min_median_dollar_volume_usd:,}"
        )
    detail["fails"] = reasons
    return Criteria(c1, c3, c4, detail)
