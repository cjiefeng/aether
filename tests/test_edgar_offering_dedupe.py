"""Issue #51: one offering filed as two same-day 424Bs counts once. Synthetic issuer (ACME,
CIK 0000000001) and synthetic accessions; no network (a fake SEC client serves the filings)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from aether.config import load_rubric, load_thesis_config
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import event_classifications, event_sources, events, filings, tickers
from aether.db.types import utcnow_iso
from aether.edgar.submissions import COLUMNS
from aether.edgar.text import offering_shares
from aether.ingest.edgar import ingest_edgar
from aether.portfolio.thesis import financings_for
from aether.providers.edgar import index_url
from tests.conftest import CONFIG_DIR

CIK = "0000000001"
DAY = "2026-10-14"
RUBRIC = load_rubric(CONFIG_DIR)


class FakeSec:
    """Serves `subs` as the submissions JSON and each accession's cover text as its document."""

    def __init__(self, rows: list[tuple[str, str, str, str]]) -> None:
        self.rows = rows  # (accession, form, filed, cover text)
        self.requests_made = 0

    def submissions(self, cik: str) -> dict[str, Any]:
        self.requests_made += 1
        recent: dict[str, list[Any]] = {c: [] for c in COLUMNS}
        for acc, form, filed, _ in self.rows:
            for c, v in (
                ("accessionNumber", acc),
                ("filingDate", filed),
                ("acceptanceDateTime", f"{filed}T21:30:00.000Z"),
                ("reportDate", ""),
                ("form", form),
                ("items", ""),
                ("primaryDocument", "d1.htm"),
                ("primaryDocDescription", form),
                ("isXBRL", 0),
            ):
                recent[c].append(v)
        return {"filings": {"recent": recent, "files": []}}

    def submissions_page(self, name: str) -> dict[str, Any]:
        raise AssertionError("no older pages")

    def document(self, cik: str, accession: str, doc: str, raw: bool = False) -> str:
        self.requests_made += 1
        text = next(t for a, _, _, t in self.rows if a == accession)
        return f"<html><body><p>{text}</p></body></html>"

    def companyfacts(self, cik: str) -> dict[str, Any]:
        self.requests_made += 1
        return {"cik": int(cik), "entityName": "ACME", "facts": {}}  # no XBRL facts


def cover(shares: str) -> str:
    return f"Prospectus supplement. We are offering {shares} shares of our common stock."


@pytest.fixture
def acme(rw_engine: Engine) -> Engine:
    with write_tx(rw_engine) as conn:
        upsert(
            conn,
            tickers,
            [{"symbol": "ACME", "type": "pure_play", "active": 1, "cik": CIK}],
            key_cols=["symbol"],
        )
    return rw_engine


def _run(engine: Engine, rows: list[tuple[str, str, str, str]]) -> None:
    ingest_edgar(engine, FakeSec(rows), RUBRIC, symbols=["ACME"], since=date(2026, 1, 1))  # type: ignore[arg-type]


def _dilution_events(engine: Engine) -> dict[int, set[str]]:
    with engine.connect() as conn:
        ids = conn.execute(
            select(events.c.id)
            .join(event_classifications, event_classifications.c.event_id == events.c.id)
            .where(event_classifications.c.category == "dilution")
        ).scalars()
        return {
            i: set(
                conn.execute(
                    select(event_sources.c.url).where(event_sources.c.event_id == i)
                ).scalars()
            )
            for i in ids
        }


A, B, C = "0000000001-26-000101", "0000000001-26-000102", "0000000001-26-000103"


def test_same_day_same_size_424b5s_are_one_event_with_two_sources(acme: Engine) -> None:
    _run(acme, [(A, "424B5", DAY, cover("20,500,000")), (B, "424B5", DAY, cover("20,500,000"))])
    evs = _dilution_events(acme)
    assert len(evs) == 1
    assert next(iter(evs.values())) == {index_url(CIK, A), index_url(CIK, B)}
    # Both filings are stored and parsed; a re-run changes nothing.
    _run(acme, [(A, "424B5", DAY, cover("20,500,000")), (B, "424B5", DAY, cover("20,500,000"))])
    assert len(_dilution_events(acme)) == 1
    with acme.connect() as conn:
        assert conn.execute(select(func.count()).select_from(filings)).scalar_one() == 2


@pytest.mark.parametrize(
    "rows",
    [
        # Different sizes: two financings.
        [(A, "424B5", DAY, cover("20,500,000")), (C, "424B5", DAY, cover("4,000,000"))],
        # Same size, different days: two financings.
        [(A, "424B5", DAY, cover("20,500,000")), (C, "424B5", "2026-10-15", cover("20,500,000"))],
        # Size not found on one cover: can't confirm it's the same offering, so no merge.
        [(A, "424B5", DAY, cover("20,500,000")), (C, "424B5", DAY, "Prospectus supplement.")],
    ],
)
def test_different_offerings_stay_separate_events(
    acme: Engine, rows: list[tuple[str, str, str, str]]
) -> None:
    _run(acme, rows)
    evs = _dilution_events(acme)
    assert len(evs) == 2 and all(len(urls) == 1 for urls in evs.values())


def _filing(acc: str, form: str, filed: str, shares: int | None) -> dict[str, Any]:
    return {
        "accession": acc,
        "symbol": "ACME",
        "cik": CIK,
        "form": form,
        "filed_at": filed,
        "url": index_url(CIK, acc),
        "items": "[]",
        "is_xbrl": 0,
        "parsed": None if shares is None else f'{{"offering": {{"shares": {shares}}}}}',
        "fetched_at": utcnow_iso(),
    }


@pytest.mark.parametrize(
    ("second", "expected"),
    [
        (_filing(B, "424B5", DAY, 20_500_000), 1),  # the IONQ case
        (_filing(B, "424B2", DAY, 20_500_000), 1),  # any primary 424B
        (_filing(B, "424B5", DAY, 4_000_000), 2),
        (_filing(B, "424B5", "2026-10-13", 20_500_000), 2),
        (_filing(B, "424B5", DAY, None), 2),
    ],
)
def test_thesis_counts_one_financing_per_offering(
    acme: Engine, second: dict[str, Any], expected: int
) -> None:
    """The thesis "repeated issuance" count reads filings directly, so it dedupes too; this also
    covers duplicates stored before the ingest fix."""
    with write_tx(acme) as conn:
        conn.execute(filings.insert(), [_filing(A, "424B5", DAY, 20_500_000), second])
    cfg = load_thesis_config(CONFIG_DIR)
    with acme.connect() as conn:
        got = financings_for(conn, "ACME", date(2026, 10, 20), cfg.financing_lookback_days)
    assert len(got) == expected
    if expected == 1:
        assert got[0]["accession"] == A and got[0]["also"] == [B]


def test_offering_shares_reads_parsed_json_and_dicts() -> None:
    assert offering_shares('{"offering": {"shares": 20500000, "prefunded": 0}}') == 20_500_000
    assert offering_shares({"offering": {"shares": 5}}) == 5
    for empty in (None, "{}", '{"offering": null}', '{"offering": {"shares": 0}}', "null"):
        assert offering_shares(empty) is None
