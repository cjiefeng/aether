"""M2 acceptance on recorded SEC responses: dilution and insider-selling events for >= 2 tickers,
the QNT lock-up from the recorded 424B4, idempotent re-runs. Documents that were not recorded
replay as 404s, which exercises the partial-failure path."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import Engine, func, select

from aether.config import load_rubric
from aether.db.models import (
    capital_structure,
    event_classifications,
    event_sources,
    event_tickers,
    events,
    filings,
    fundamentals_q,
    insider_txns,
    lockups,
)
from aether.ingest.edgar import ingest_edgar
from aether.providers.edgar import EdgarClient
from aether.providers.prices import ProviderError
from aether.risk.flags import open_flags
from tests.cassettes import mount_cassette
from tests.conftest import CONFIG_DIR, seed_tickers

UA = "Aether test suite tests@example.test"
CIKS = {"IONQ": "0001824920", "RGTI": "0001838359", "QBTS": "0001907982", "QNT": "0002110105"}
CASSETTES = [
    "sec_submissions_qbts",
    "sec_submissions_ionq",
    "sec_submissions_rgti",
    "sec_submissions_qnt",
    "sec_form4_rgti_sale",
    "sec_form4_rgti_sale_10b5_1",
    "sec_form4_ionq_sale_10b5_1",
    "sec_form4_ionq_tax_withholding",
    "sec_424b4_qnt",
    "sec_companyfacts_qnt",
]


@pytest.fixture
def seeded(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, [(s, "pure_play") for s in CIKS])
    from sqlalchemy import update

    from aether.db.engine import write_tx
    from aether.db.models import tickers

    with write_tx(rw_engine) as conn:
        for s, cik in CIKS.items():
            conn.execute(update(tickers).where(tickers.c.symbol == s).values(cik=cik))
    return rw_engine


@pytest.fixture
def sec() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        for name in CASSETTES:
            mount_cassette(router, name)
        # Anything not recorded: SEC answers 404 (a permanent error, not retried).
        router.route(host__regex=r"^(www|data)\.sec\.gov$").mock(return_value=httpx.Response(404))
        yield router


def client() -> EdgarClient:
    return EdgarClient(UA, min_interval_s=0, sleep=lambda _s: None)


def _count(engine: Engine, table: object) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())  # type: ignore[arg-type]


def _events(engine: Engine) -> list[tuple[str, str, str, str, int]]:
    with engine.connect() as conn:
        return [
            tuple(r)  # type: ignore[misc]
            for r in conn.execute(
                select(
                    event_tickers.c.symbol,
                    filings.c.form,
                    event_classifications.c.category,
                    event_classifications.c.rule_id,
                    event_classifications.c.materiality,
                )
                .join(events, events.c.id == event_tickers.c.event_id)
                .join(event_classifications, event_classifications.c.event_id == events.c.id)
                .join(filings, filings.c.accession == events.c.accession)
            ).all()
        ]


def test_ingest_flags_dilution_insiders_and_qnt_lockup(
    seeded: Engine, sec: respx.MockRouter
) -> None:
    res = ingest_edgar(seeded, client(), load_rubric(CONFIG_DIR))
    assert res.provider == "sec"
    assert res.warning and "404" in res.warning  # unrecorded documents are skipped, not fatal

    ev = _events(seeded)
    dilution = {(s, f) for s, f, c, *_ in ev if c == "dilution"}
    # Known dilution filings for >= 2 tickers.
    assert ("QBTS", "S-3ASR") in dilution
    assert ("QBTS", "424B7") in dilution
    assert ("IONQ", "424B5") in dilution
    assert ("QNT", "424B4") in dilution
    insider = {s for s, f, c, *_ in ev if c == "insider_selling"}
    assert {"RGTI", "IONQ"} <= insider

    by_rule = {(s, f): (rule, m) for s, f, _c, rule, m in ev}
    assert by_rule[("IONQ", "424B5")] == ("edgar_primary_prospectus", 4)
    assert by_rule[("QBTS", "424B7")] == ("edgar_resale_prospectus", 2)

    with seeded.connect() as conn:
        lk = conn.execute(select(lockups)).one()
        assert (lk.symbol, lk.prospectus_date, lk.lockup_days, lk.expiry_date) == (
            "QNT",
            "2026-06-03",
            180,
            "2026-11-30",
        )
        assert lk.early_release_possible == 1
        # The tax-withholding Form 4 was parsed but produced no event.
        f_row = conn.execute(
            select(filings.c.parsed).where(filings.c.accession == "0001193125-26-392335")
        ).scalar_one()
        assert json.loads(f_row)["form4_txns"] == 1
        assert (
            conn.execute(
                select(func.count())
                .select_from(events)
                .where(events.c.accession == "0001193125-26-392335")
            ).scalar_one()
            == 0
        )
        # Plan sales carry the lower materiality.
        m = conn.execute(
            select(event_classifications.c.materiality)
            .join(events, events.c.id == event_classifications.c.event_id)
            .where(events.c.accession == "0001123292-26-001272")
        ).scalar_one()
        assert m == 1
        src = conn.execute(select(event_sources.c.domain, event_sources.c.trust_tier)).all()
        assert set(src) == {("sec.gov", "T1")}
        assert conn.execute(select(func.count()).select_from(fundamentals_q)).scalar_one() > 0
        shelves = conn.execute(
            select(capital_structure.c.symbol).where(capital_structure.c.instrument == "shelf")
        ).scalars()
        assert "QBTS" in set(shelves)

    # The lock-up is within the 60-day window on 2026-10-04 -> open flag.
    rubric = load_rubric(CONFIG_DIR)
    flags = open_flags(seeded, rubric.risk_flags, date(2026, 10, 4), list(CIKS))
    lk_flags = [f for f in flags if f.kind == "lockup_expiry"]
    assert [(f.symbol, f.as_of) for f in lk_flags] == [("QNT", "2026-11-30")]
    early = open_flags(seeded, rubric.risk_flags, date(2026, 9, 1), ["QNT"])  # 90 days out
    assert not [f for f in early if f.kind == "lockup_expiry"]


def test_ingest_is_idempotent(seeded: Engine, sec: respx.MockRouter) -> None:
    rubric = load_rubric(CONFIG_DIR)
    ingest_edgar(seeded, client(), rubric)
    tables = (filings, events, event_classifications, insider_txns, lockups, capital_structure)
    first = [_count(seeded, t) for t in tables]
    c2 = client()
    ingest_edgar(seeded, c2, rubric)
    assert [_count(seeded, t) for t in tables] == first
    # Recorded documents were parsed once; only the 404s are retried.
    assert c2.requests_made < 400


def test_all_symbols_failing_raises(seeded: Engine) -> None:
    with respx.mock() as router:
        router.route(host="data.sec.gov").mock(return_value=httpx.Response(403))
        with pytest.raises(ProviderError):
            ingest_edgar(seeded, client(), load_rubric(CONFIG_DIR))
