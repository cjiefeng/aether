"""QTUM holdings: parsing (synthetic fixture), sanity checks, robots.txt gate, idempotency."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine, func, select

from aether.db.models import qtum_holdings
from aether.ingest.qtum_holdings import (
    QTUM_HOLDINGS_URL,
    HoldingsError,
    fetch_holdings,
    ingest_qtum_holdings,
    parse_holdings,
    robots_allows,
)

FIXTURE = Path(__file__).parent / "fixtures" / "html" / "qtum_holdings_synthetic.html"
ROBOTS_URL = "https://www.defianceetfs.com/robots.txt"
ALLOW_ALL = "User-agent: *\nDisallow:\n"


def html() -> str:
    return FIXTURE.read_text("utf-8")


def test_parse_synthetic_page() -> None:
    snap = parse_holdings(html())
    assert snap.as_of == date(2026, 10, 5)
    by_sym = {h.symbol: h for h in snap.holdings}
    assert set(by_sym) == {"ACME", "EXMP", "9999 ZZ", "Cash&Other"}
    assert by_sym["ACME"].weight == 40.0 and by_sym["ACME"].shares == 1_000_000
    assert by_sym["EXMP"].name == "Example & Co"
    assert snap.weight_sum == pytest.approx(100.0)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda h: h.replace('id="table-full-holdings"', 'id="other"'), "not found"),
        (lambda h: h.replace("Data as of", "Updated"), "as of"),
        (lambda h: h.replace("40.00%", "4.00%"), "sum"),
        (lambda h: h.replace("<th>Shares</th>", "<th>Qty</th>"), "header"),
        (lambda h: h.replace("35.50%", "35.50"), "weight without %"),
        (lambda h: h.replace("<td>EXMP</td>", "<td>ACME</td>"), "duplicate"),
    ],
)
def test_sanity_checks_reject(mutate: object, message: str) -> None:
    with pytest.raises(HoldingsError, match=message):
        parse_holdings(mutate(html()))  # type: ignore[operator]


@respx.mock
def test_robots_disallow_means_no_fetch() -> None:
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    page = respx.get(QTUM_HOLDINGS_URL).mock(return_value=httpx.Response(200, text=html()))
    with httpx.Client() as c, pytest.raises(HoldingsError, match="robots"):
        fetch_holdings(c)
    assert not page.called


@respx.mock
def test_robots_fail_closed_on_error() -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(503))
    with httpx.Client() as c:
        assert robots_allows(c, QTUM_HOLDINGS_URL) is False
    respx.get(ROBOTS_URL).mock(side_effect=httpx.ConnectError("synthetic"))
    with httpx.Client() as c:
        assert robots_allows(c, QTUM_HOLDINGS_URL) is False


@respx.mock
def test_robots_404_means_allowed() -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(404))
    with httpx.Client() as c:
        assert robots_allows(c, QTUM_HOLDINGS_URL) is True


@respx.mock
def test_ingest_is_idempotent_and_replaces_snapshot(rw_engine: Engine) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    page = respx.get(QTUM_HOLDINGS_URL).mock(return_value=httpx.Response(200, text=html()))
    with httpx.Client() as c:
        assert ingest_qtum_holdings(rw_engine, c).rows_written == 4
        ingest_qtum_holdings(rw_engine, c)
        # Same "as of" date; one holding replaced by another with the same weight.
        page.mock(return_value=httpx.Response(200, text=html().replace("9999 ZZ", "NEWCO")))
        ingest_qtum_holdings(rw_engine, c)
    with rw_engine.connect() as conn:
        syms = set(conn.execute(select(qtum_holdings.c.holding_symbol)).scalars())
        n = conn.execute(select(func.count()).select_from(qtum_holdings)).scalar_one()
    assert n == 4 and syms == {"ACME", "EXMP", "NEWCO", "Cash&Other"}
    # Only a generic UA goes to the issuer.
    assert page.calls.last.request.headers["user-agent"].startswith("aether/")
