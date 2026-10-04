"""Insider-cluster detection, open risk flags and the earnings calendar (synthetic ACME data)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, update

from aether.config import load_rubric
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import capital_structure, earnings_calendar, filings, insider_txns
from aether.db.types import utcnow_iso
from aether.ingest.earnings_calendar import ingest_earnings_calendar, upcoming_from_calendar
from aether.providers.prices import ProviderError
from aether.risk.flags import Sale, cluster_in_window, open_flags
from tests.conftest import CONFIG_DIR, seed_tickers

PARAMS = load_rubric(CONFIG_DIR).risk_flags
END = date(2026, 3, 31)


def s(key: str, days_before: int, plan: bool = False) -> Sale:
    return Sale(key, f"Insider {key}", END - timedelta(days=days_before), 100, plan)


def test_cluster_needs_three_distinct_insiders_in_window() -> None:
    assert cluster_in_window([s("a", 1), s("b", 2), s("a", 3)], END, 30, 3) is None
    c = cluster_in_window([s("a", 1), s("b", 2), s("c", 29, plan=True)], END, 30, 3)
    assert c is not None and c.n_insiders == 3 and c.plan_sales == 1 and c.shares == 300
    # (end - 30d, end]: a sale exactly 30 days before is outside the window.
    assert cluster_in_window([s("a", 1), s("b", 2), s("c", 30)], END, 30, 3) is None


def test_cluster_keys_on_cik_not_name() -> None:
    same_name = [Sale("1", "Doe", END, 1, False), Sale("2", "Doe", END, 1, False), s("c", 0)]
    c = cluster_in_window(same_name, END, 30, 3)
    assert c is not None and c.n_insiders == 3 and c.insiders == ("Doe", "Insider c")


def _filing(
    acc: str,
    form: str,
    filed: str,
    parsed: dict[str, object] | None = None,
    items: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "accession": acc,
        "symbol": "ACME",
        "cik": "0000000001",
        "form": form,
        "filed_at": filed,
        "accepted_at": None,
        "report_date": None,
        "items": json.dumps(list(items)),
        "primary_doc": "acme.htm",
        "primary_doc_description": None,
        "url": f"https://www.sec.gov/{acc}-index.htm",
        "is_xbrl": 0,
        "parsed": json.dumps(parsed) if parsed is not None else None,
        "fetched_at": utcnow_iso(),
    }


@pytest.fixture
def acme(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, [("ACME", "pure_play")])
    fs = [
        _filing("0000000001-26-000001", "4", "2026-03-20", {"form4_txns": 3}),
        _filing("0000000001-26-000002", "424B5", "2026-02-01", {"atm": {}}),
        _filing("0000000001-25-000003", "10-K", "2025-03-01", {"going_concern": True}),
        _filing("0000000001-26-000004", "10-Q", "2026-03-01", {"going_concern": False}),
        _filing("0000000001-26-000005", "8-K", "2026-02-25", None, ("2.02", "9.01")),
        _filing("0000000001-26-000006", "8-K/A", "2026-02-27", None, ("2.02",)),
    ]
    txns = [
        {
            "accession": "0000000001-26-000001",
            "seq": i,
            "symbol": "ACME",
            "insider_cik": str(i),
            "insider": f"Insider {i}",
            "role": None,
            "security": "Common",
            "txn_date": d,
            "code": "S",
            "acquired_disposed": "D",
            "shares": 1000,
            "price": 2.0,
            "is_10b5_1": int(i == 0),
            "is_derivative": 0,
        }
        for i, d in enumerate(["2026-03-10", "2026-03-15", "2026-03-20"])
    ]
    atm = {
        "symbol": "ACME",
        "as_of": "2026-02-01",
        "instrument": "atm",
        "source_accession": "0000000001-26-000002",
        "amount_micros": None,
        "shares_underlying": None,
        "strike_micros": None,
        "source": "filing_text",
        "concept": None,
        "excerpt": "at-the-market",
    }
    with write_tx(rw_engine) as conn:
        upsert(conn, filings, fs, key_cols=["accession"])
        upsert(conn, insider_txns, txns, key_cols=["accession", "seq"])
        upsert(
            conn,
            capital_structure,
            [atm],
            key_cols=["symbol", "as_of", "instrument", "source_accession"],
        )
    return rw_engine


def test_open_flags(acme: Engine) -> None:
    flags = {f.kind: f for f in open_flags(acme, PARAMS, END, ["ACME"])}
    assert set(flags) == {"insider_cluster", "active_atm"}  # latest 10-Q has no going concern
    assert "3 insiders sold 3,000 shares" in flags["insider_cluster"].detail
    assert "(1 of 3 sales under 10b5-1 plans)" in flags["insider_cluster"].detail
    later = {f.kind for f in open_flags(acme, PARAMS, date(2027, 6, 1), ["ACME"])}
    assert later == set()


def test_going_concern_flag_uses_latest_periodic_report(acme: Engine) -> None:
    # The older 10-K had going-concern language; the latest 10-Q doesn't -> no flag (above).
    with write_tx(acme) as conn:
        conn.execute(
            update(filings)
            .where(filings.c.accession == "0000000001-26-000004")
            .values(parsed=json.dumps({"going_concern": True}))
        )
    flags = {f.kind: f for f in open_flags(acme, PARAMS, END, ["ACME"])}
    assert flags["going_concern"].as_of == "2026-03-01"
    assert "10-Q" in flags["going_concern"].detail


def test_upcoming_from_yfinance_calendar_shapes() -> None:
    assert upcoming_from_calendar({"Earnings Date": [date(2026, 11, 5), date(2026, 11, 5)]}) == [
        date(2026, 11, 5)
    ]
    assert upcoming_from_calendar({"Earnings Date": datetime(2026, 11, 5, 20)}) == [
        date(2026, 11, 5)
    ]
    assert upcoming_from_calendar({}) == []
    assert upcoming_from_calendar(None) == []


def test_earnings_calendar_reported_and_scheduled(acme: Engine) -> None:
    today = date(2026, 4, 1)
    calls = iter([[date(2026, 5, 7), date(2025, 1, 1)], [date(2026, 5, 12)]])
    ingest_earnings_calendar(acme, lambda _s: next(calls), today=today)
    ingest_earnings_calendar(acme, lambda _s: next(calls), today=today)  # date moved
    with acme.connect() as conn:
        rows = conn.execute(
            select(
                earnings_calendar.c.date, earnings_calendar.c.status, earnings_calendar.c.source
            ).order_by(earnings_calendar.c.date)
        ).all()
    assert [tuple(r) for r in rows] == [
        ("2026-02-25", "reported", "8k_2.02"),  # the 8-K/A two days later is the same release
        ("2026-02-27", "reported", "8k_2.02"),
        ("2026-05-12", "scheduled", "yfinance"),
    ]


def test_earnings_calendar_all_failures_raise(acme: Engine) -> None:
    def boom(_s: str) -> list[date]:
        raise RuntimeError("rate limited")

    with pytest.raises(ProviderError):
        ingest_earnings_calendar(acme, boom, today=date(2026, 4, 1))
