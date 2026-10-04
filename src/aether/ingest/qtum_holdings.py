"""QTUM holdings snapshot from Defiance's public full-holdings page (spec §4).

The page is a server-rendered `<table id="table-full-holdings">` (Ticker, Name, CUSIP,
ETF Weight, Shares) plus a "Data as of MM/DD/YYYY" line. Parsed with the stdlib HTML parser.

S6: robots.txt is checked before every fetch (fail closed if it can't be read), and a generic
User-Agent is sent (never SEC_USER_AGENT). Nothing from the page reaches an LLM.
"""

from __future__ import annotations

import logging
import re
import urllib.robotparser
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
from sqlalchemy import Engine, delete

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import qtum_holdings
from aether.db.types import utcnow_iso
from aether.runs import JobResult

log = logging.getLogger(__name__)

QTUM_HOLDINGS_URL = "https://www.defianceetfs.com/qtum-full-holdings/"
USER_AGENT = "aether/0.1 (self-hosted personal research)"
TABLE_ID = "table-full-holdings"
EXPECTED_HEADER = ("Ticker", "Name", "CUSIP", "ETF Weight", "Shares")
WEIGHT_SUM_RANGE = (95.0, 105.0)
_AS_OF = re.compile(r"Data as of\s+(\d{1,2})/(\d{1,2})/(\d{4})")


class HoldingsError(Exception):
    """The page could not be fetched, was disallowed, or failed a sanity check."""


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    cusip: str
    weight: float  # percent of fund
    shares: int | None


@dataclass(frozen=True)
class HoldingsSnapshot:
    as_of: date
    holdings: tuple[Holding, ...]

    @property
    def weight_sum(self) -> float:
        return sum(h.weight for h in self.holdings)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.depth = 0  # nested <table> depth inside ours
        self.cell: list[str] | None = None
        self.row: list[str] | None = None
        self.rows: list[list[str]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self.in_table:
                self.depth += 1
            elif dict(attrs).get("id") == TABLE_ID:
                self.in_table = True
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.in_table:
            if self.depth:
                self.depth -= 1
            else:
                self.in_table = False
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)
        self.text.append(data)


def _parse_weight(s: str) -> float:
    if not s.endswith("%"):
        raise HoldingsError(f"weight without %: {s[:20]!r}")
    return float(s[:-1].replace(",", ""))


def _parse_shares(s: str) -> int | None:
    s = s.replace(",", "").strip()
    if not s:
        return None
    return round(float(s))


def parse_holdings(html: str) -> HoldingsSnapshot:
    p = _TableParser()
    p.feed(html)
    p.close()
    if not p.rows:
        raise HoldingsError(f"table #{TABLE_ID} not found")
    header, body = tuple(p.rows[0]), p.rows[1:]
    if header != EXPECTED_HEADER:
        raise HoldingsError(f"unexpected header {header!r}")
    m = _AS_OF.search(" ".join(" ".join(p.text).split()))
    if not m:
        raise HoldingsError("'Data as of' date not found")
    month, day, year = (int(g) for g in m.groups())
    as_of = date(year, month, day)

    holdings: list[Holding] = []
    for row in body:
        if len(row) != len(EXPECTED_HEADER):
            raise HoldingsError(f"row has {len(row)} cells, expected {len(EXPECTED_HEADER)}")
        sym, name, cusip, weight, shares = row
        if not sym:
            raise HoldingsError("row without ticker")
        try:
            holdings.append(Holding(sym, name, cusip, _parse_weight(weight), _parse_shares(shares)))
        except ValueError as exc:
            raise HoldingsError(f"bad number in row {sym!r}") from exc
    snap = HoldingsSnapshot(as_of, tuple(holdings))
    if not holdings:
        raise HoldingsError("no holdings rows")
    lo, hi = WEIGHT_SUM_RANGE
    if not lo <= snap.weight_sum <= hi:
        raise HoldingsError(f"weights sum to {snap.weight_sum:.2f}%, outside {lo}-{hi}%")
    if len({h.symbol for h in holdings}) != len(holdings):
        raise HoldingsError("duplicate tickers in holdings table")
    return snap


def robots_allows(client: httpx.Client, url: str, user_agent: str = USER_AGENT) -> bool:
    """Fail closed: network errors or 5xx mean "not allowed". A 4xx means no robots.txt."""
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        resp = client.get(robots_url, headers={"User-Agent": user_agent})
    except httpx.HTTPError:
        return False
    if 400 <= resp.status_code < 500:
        return True
    if resp.status_code != 200:
        return False
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(resp.text.splitlines())
    return rp.can_fetch(user_agent, url)


def fetch_holdings(client: httpx.Client, url: str = QTUM_HOLDINGS_URL) -> HoldingsSnapshot:
    if not robots_allows(client, url):
        raise HoldingsError(f"robots.txt disallows (or could not be read for) {url}")
    try:
        resp = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise HoldingsError(f"fetch failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise HoldingsError(f"HTTP {resp.status_code}")
    return parse_holdings(resp.text)


def ingest_qtum_holdings(engine: Engine, client: httpx.Client) -> JobResult:
    snap = fetch_holdings(client)  # network first, outside any transaction
    fetched_at = utcnow_iso()
    rows = [
        {
            "snapshot_date": snap.as_of.isoformat(),
            "holding_symbol": h.symbol,
            "name": h.name,
            "cusip": h.cusip,
            "weight": h.weight,
            "shares": h.shares,
            "fetched_at": fetched_at,
        }
        for h in snap.holdings
    ]
    keep = [h.symbol for h in snap.holdings]
    with write_tx(engine) as conn:
        # A snapshot date is replaced wholesale: drop holdings no longer listed for that date.
        conn.execute(
            delete(qtum_holdings).where(
                qtum_holdings.c.snapshot_date == snap.as_of.isoformat(),
                qtum_holdings.c.holding_symbol.not_in(keep),
            )
        )
        upsert(conn, qtum_holdings, rows, key_cols=["snapshot_date", "holding_symbol"])
    log.info("qtum holdings %s: %d rows (sum %.2f%%)", snap.as_of, len(rows), snap.weight_sum)
    return JobResult(rows_written=len(rows), provider="defiance")
