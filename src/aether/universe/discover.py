"""Candidate discovery (spec §6.7 pipeline step 1): deterministic, SEC data only, no LLM.

Three sources, merged by CIK:
- **EDGAR full-text search** (`efts.sec.gov`): the configured exact phrases in the configured
  forms over the last N months. Each hit names one or more filer CIKs (an S-4 names the SPAC and
  its target), with SEC's display name, tickers and SIC codes.
- **QTUM holdings** not on the watchlist (latest `qtum_holdings` snapshot), mapped ticker → CIK
  through SEC's `company_tickers_exchange.json`. Lines that don't map to a US listing are only
  counted ("outside mandate").
- **Every active pure-play** on the watchlist (the same test applies to current names).

Watchlist symbols of every type (context, benchmark, ETF) are never new candidates, nor are
excluded SIC codes (blank checks). Nothing here is model output.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, func, select

from aether.config import Watchlist
from aether.db.models import qtum_holdings

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
# "Quantinuum Inc.  (QNT)  (CIK 0002110105)" / "IonQ, Inc.  (IONQ, IONQ-WT)  (CIK 0001824920)"
_DISPLAY_RE = re.compile(
    r"^(?P<name>.*?)\s*(?:\((?P<tickers>[^()]*)\))?\s*\(CIK (?P<cik>\d{10})\)\s*$"
)
OFFERING_FORMS = ("S-1", "S-1/A", "F-1", "F-1/A", "424B4")


@dataclass(frozen=True)
class Listing:
    cik: str  # 10 digits
    name: str
    ticker: str
    exchange: str


@dataclass(frozen=True)
class ExchangeMap:
    """SEC's ticker ↔ CIK ↔ exchange map (criterion 1's source of truth)."""

    by_ticker: Mapping[str, Listing]
    by_cik: Mapping[str, tuple[Listing, ...]]

    def listing(self, cik: str, exchanges: Iterable[str]) -> Listing | None:
        """The CIK's first listing on an allowed exchange (common stock tickers sort first:
        SEC lists the primary ticker before warrants/units)."""
        allowed = set(exchanges)
        for x in self.by_cik.get(cik, ()):
            if x.exchange in allowed:
                return x
        return None


def parse_exchange_map(raw: Mapping[str, Any]) -> ExchangeMap:
    fields = raw.get("fields")
    data = raw.get("data")
    if fields != ["cik", "name", "ticker", "exchange"] or not isinstance(data, list):
        raise ValueError("company_tickers_exchange.json: unexpected shape")
    by_ticker: dict[str, Listing] = {}
    by_cik: dict[str, list[Listing]] = {}
    for row in data:
        if not isinstance(row, list) or len(row) != 4:
            continue
        cik, name, ticker, exchange = row
        if not isinstance(cik, int) or not isinstance(ticker, str) or not isinstance(name, str):
            continue
        ticker = ticker.upper()
        if not TICKER_RE.fullmatch(ticker):
            continue
        x = Listing(f"{cik:010d}", name, ticker, exchange if isinstance(exchange, str) else "")
        by_ticker.setdefault(ticker, x)
        by_cik.setdefault(x.cik, []).append(x)
    return ExchangeMap(by_ticker, {k: tuple(v) for k, v in by_cik.items()})


@dataclass
class FtsEntity:
    cik: str
    name: str
    tickers: tuple[str, ...]
    sics: set[str] = field(default_factory=set)
    # (form, file_date, accession, primary document, co-registrant filing?)
    filings: list[tuple[str, str, str, str, bool]] = field(default_factory=list)

    def offering_alone(self) -> bool:
        """Filed an S-1/F-1/424B4 as the only registrant (an IPO in progress), as opposed to
        appearing as a co-registrant on someone else's S-4."""
        return any(f in OFFERING_FORMS and not co for f, _d, _a, _p, co in self.filings)


def parse_display_name(s: str) -> tuple[str, tuple[str, ...], str] | None:
    m = _DISPLAY_RE.match(" ".join(s.split()) if s else "")
    if not m:
        return None
    tickers = tuple(
        t.strip().upper()
        for t in (m.group("tickers") or "").split(",")
        if TICKER_RE.fullmatch(t.strip().upper())
    )
    return m.group("name").strip(), tickers, m.group("cik")


def parse_fts_hits(hits: Sequence[Mapping[str, Any]]) -> dict[str, FtsEntity]:
    out: dict[str, FtsEntity] = {}
    for h in hits:
        src = h.get("_source")
        hid = h.get("_id")
        if not isinstance(src, Mapping) or not isinstance(hid, str) or ":" not in hid:
            continue
        accession, doc = hid.split(":", 1)
        names = src.get("display_names") or []
        sics = [str(s) for s in src.get("sics") or [] if s]
        form, filed = str(src.get("form") or ""), str(src.get("file_date") or "")
        co = len(names) > 1
        for i, dn in enumerate(names):
            parsed = parse_display_name(str(dn))
            if parsed is None:
                continue
            name, tickers, cik = parsed
            e = out.setdefault(cik, FtsEntity(cik, name, tickers))
            if tickers and not e.tickers:
                e.tickers = tickers
            if i < len(sics):
                e.sics.add(sics[i])
            e.filings.append((form, filed, accession, doc, co))
    for e in out.values():
        e.filings.sort(key=lambda f: (f[1], f[2]), reverse=True)
    return out


@dataclass
class Candidate:
    """One company the review looks at. `symbol` is the listed ticker, else the ticker SEC shows
    for an IPO filer, else `CIK<10 digits>`."""

    symbol: str
    cik: str | None
    name: str
    sources: set[str] = field(default_factory=set)  # fts | qtum | watchlist
    listing: Listing | None = None
    on_watchlist: bool = False
    offering_alone: bool = False  # an IPO filing of its own on EDGAR (announced listing)
    qtum_weight: float | None = None  # percent of fund, latest snapshot

    @property
    def announced(self) -> bool:
        return self.listing is None and self.offering_alone


@dataclass
class Discovery:
    candidates: list[Candidate]
    screened: list[dict[str, Any]]  # {symbol, name, reason}
    counts: dict[str, int]


def latest_qtum(engine: Engine) -> tuple[str | None, dict[str, float]]:
    with engine.connect() as conn:
        d = conn.execute(select(func.max(qtum_holdings.c.snapshot_date))).scalar()
        if d is None:
            return None, {}
        rows = conn.execute(
            select(qtum_holdings.c.holding_symbol, qtum_holdings.c.weight).where(
                qtum_holdings.c.snapshot_date == d
            )
        ).all()
    return d, {str(s).upper(): float(w) for s, w in rows}


def discover(
    watchlist: Watchlist,
    exchange_map: ExchangeMap,
    fts: Mapping[str, FtsEntity],
    qtum: Mapping[str, float],
    *,
    exchanges: Sequence[str],
    excluded_sics: Sequence[str],
) -> Discovery:
    watch_symbols = {t.symbol for t in watchlist.tickers}
    watch_ciks = {t.cik for t in watchlist.tickers if t.cik}
    pure = [t for t in watchlist.tickers if t.type == "pure_play" and t.active]
    by_cik: dict[str, Candidate] = {}
    screened: list[dict[str, Any]] = []
    counts = {"fts_entities": len(fts), "qtum_lines": len(qtum), "outside_mandate": 0}

    for t in pure:
        assert t.cik is not None  # pure-plays carry a CIK (watchlist loader)
        listing = exchange_map.listing(t.cik, exchanges)
        by_cik[t.cik] = Candidate(
            symbol=t.symbol,
            cik=t.cik,
            name=(listing.name if listing else (t.aliases[0] if t.aliases else t.symbol)),
            sources={"watchlist"},
            listing=listing,
            on_watchlist=True,
            qtum_weight=qtum.get(t.symbol),
        )

    for cik, e in sorted(fts.items()):
        if cik in by_cik:
            by_cik[cik].sources.add("fts")
            continue
        if cik in watch_ciks:
            continue
        listing = exchange_map.listing(cik, exchanges)
        symbol = listing.ticker if listing else (e.tickers[0] if e.tickers else f"CIK{cik}")
        if symbol in watch_symbols:
            continue
        if set(excluded_sics) & e.sics:
            screened.append(
                {"symbol": symbol, "name": e.name, "reason": "excluded SIC (blank check)"}
            )
            continue
        if listing is None and not e.offering_alone():
            screened.append(
                {"symbol": symbol, "name": e.name, "reason": "not US-listed and no IPO filing"}
            )
            continue
        by_cik[cik] = Candidate(
            symbol=symbol,
            cik=cik,
            name=listing.name if listing else e.name,
            sources={"fts"},
            listing=listing,
            offering_alone=e.offering_alone(),
            qtum_weight=qtum.get(symbol),
        )

    for sym, weight in sorted(qtum.items()):
        if sym in watch_symbols:
            continue
        x = exchange_map.by_ticker.get(sym)
        if x is None or x.exchange not in set(exchanges):
            counts["outside_mandate"] += 1
            continue
        if x.cik in watch_ciks:
            continue
        if x.cik in by_cik:
            by_cik[x.cik].sources.add("qtum")
            by_cik[x.cik].qtum_weight = weight
            continue
        by_cik[x.cik] = Candidate(
            symbol=x.ticker,
            cik=x.cik,
            name=x.name,
            sources={"qtum"},
            listing=exchange_map.listing(x.cik, exchanges),
            qtum_weight=weight,
        )

    ordered = sorted(by_cik.values(), key=lambda c: (not c.on_watchlist, c.symbol))
    counts["candidates"] = len(ordered)
    return Discovery(ordered, screened, counts)
