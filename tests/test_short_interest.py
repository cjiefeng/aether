"""FINRA short interest (M8): the recorded real files (filtered to the universe), the spike rule on
synthetic ACME data, the settlement-date candidates, the open flag and idempotency. Network is
blocked; the provider is replaced by an in-memory file map."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, insert, select

from aether.config import ShortInterestRule, load_rubric
from aether.db.engine import write_tx
from aether.db.models import (
    event_classifications,
    events,
    fundamentals_q,
    short_interest,
    short_interest_files,
)
from aether.ingest.short_interest import ingest_short_interest
from aether.providers.finra import FinraShortInterest, candidates, file_url, nominal_dates, parse
from aether.risk.flags import short_interest_flags
from tests.conftest import CONFIG_DIR, seed_tickers

FIX = Path(__file__).parent / "fixtures" / "finra"
RULE = load_rubric(CONFIG_DIR).short_interest
UNIVERSE = {"QTUM", "IONQ", "QNT", "RGTI", "QBTS", "INFQ"}
HEADER = (
    "accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|"
    "marketClassCode|currentShortPositionQuantity|previousShortPositionQuantity|stockSplitFlag|"
    "averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag|changePercent|"
    "changePreviousNumber|settlementDate"
)


def acme_file(d: str, short: int, prev: int = 0) -> str:
    """A synthetic FINRA-format file (quoted, as FINRA sometimes serves it) with one ACME row."""
    ymd = d.replace("-", "")
    cells = [ymd, "ACME", "Acme Synthetic Inc.", "A", "NYSE", str(short), str(prev), None,
             "1000000", "2.50", None, "0", "0", d]  # fmt: skip
    line = "|".join("" if c is None else f'"{c}"' for c in cells)
    quoted_header = "|".join(f'"{c}"' for c in HEADER.split("|"))
    return f"{quoted_header}\n{line}\n"


def transport(files: dict[str, str]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        body = files.get(str(req.url))
        return httpx.Response(200, text=body) if body is not None else httpx.Response(403)

    return httpx.MockTransport(handler)


def provider(files: dict[str, str]) -> FinraShortInterest:
    return FinraShortInterest(client=httpx.Client(transport=transport(files)))


def seed_shares(engine: Engine, symbol: str, shares: int, as_of: str = "2026-07-31") -> None:
    with write_tx(engine) as conn:
        conn.execute(
            insert(fundamentals_q).values(
                symbol=symbol,
                period_end=as_of,
                concept="dei:EntityCommonStockSharesOutstanding",
                period_days=0,
                value_int=shares,
                unit="shares",
            )
        )


# --------------------------------------------------------------------------- provider


def test_recorded_files_parse_universe_rows() -> None:
    rows = parse((FIX / "shrt20260915.csv").read_text("utf-8"), UNIVERSE)
    assert {r.symbol for r in rows} == UNIVERSE
    ionq = next(r for r in rows if r.symbol == "IONQ")
    assert ionq.settlement_date == date(2026, 9, 15)
    assert ionq.short_shares == 40_486_062 and ionq.prev_short_shares == 41_053_368
    assert ionq.days_to_cover == pytest.approx(2.19)


def test_quoted_format_parses_and_unknown_columns_fail() -> None:
    rows = parse(acme_file("2026-09-15", 5), {"ACME"})
    assert [(r.symbol, r.short_shares) for r in rows] == [("ACME", 5)]
    with pytest.raises(Exception, match="unexpected columns"):
        parse("a|b\n1|2\n", {"ACME"})


def test_settlement_candidates_step_back_over_weekends() -> None:
    # 2026-08-15 was a Saturday: try Sat → Fri 14th first among weekdays.
    assert candidates(date(2026, 8, 15))[0] == date(2026, 8, 14)
    assert all(d.weekday() < 5 for d in candidates(date(2026, 8, 15)))
    # The current month plus `months_back` earlier months; future dates are left out.
    got = nominal_dates(date(2026, 10, 5), 1)
    assert got == [date(2026, 9, 30), date(2026, 9, 15)]


# --------------------------------------------------------------------------- ingest + rule


def run(engine: Engine, files: dict[str, str], today: date = date(2026, 10, 5)) -> int:
    rule = RULE.model_copy(update={"months_back": 2})
    now = datetime(today.year, today.month, today.day, 1, tzinfo=UTC)
    return ingest_short_interest(engine, provider(files), rule, today=today, now=now).rows_written


def test_recorded_files_ingest_with_pct_of_shares_outstanding(rw_engine: Engine) -> None:
    seed_tickers(rw_engine, [("QTUM", "etf"), ("IONQ", "pure_play"), ("RGTI", "pure_play")])
    seed_shares(rw_engine, "IONQ", 300_000_000)
    files = {
        file_url(date(2026, 8, 31)): (FIX / "shrt20260831.csv").read_text("utf-8"),
        file_url(date(2026, 9, 15)): (FIX / "shrt20260915.csv").read_text("utf-8"),
    }
    run(rw_engine, files)
    with rw_engine.connect() as conn:
        rows = {
            (r.symbol, r.settlement_date): r for r in conn.execute(select(short_interest)).all()
        }
        n_files = conn.execute(select(short_interest_files)).all()
    assert set(rows) == {
        (s, d) for s in ("QTUM", "IONQ", "RGTI") for d in ("2026-08-31", "2026-09-15")
    }
    ionq = rows[("IONQ", "2026-09-15")]
    assert ionq.pct_shares_out == pytest.approx(40_486_062 / 300_000_000 * 100, abs=1e-4)
    assert ionq.shares_out_as_of == "2026-07-31"
    assert rows[("RGTI", "2026-09-15")].pct_shares_out is None  # no XBRL share count
    assert len(n_files) == 2


def test_spike_rule_fires_on_rise_once_and_is_idempotent(rw_engine: Engine) -> None:
    """Acceptance: the short-interest spike rule fires on a fixture."""
    seed_tickers(rw_engine, [("ACME", "pure_play")])
    seed_shares(rw_engine, "ACME", 100_000_000)
    files = {
        file_url(date(2026, 8, 31)): acme_file("2026-08-31", 10_000_000),  # 10%
        file_url(date(2026, 9, 15)): acme_file("2026-09-15", 16_000_000),  # 16%: +6 pp
    }
    run(rw_engine, files)
    run(rw_engine, files)  # nothing new to fetch, nothing duplicated
    with rw_engine.connect() as conn:
        evs = conn.execute(
            select(
                events, event_classifications.c.category, event_classifications.c.materiality
            ).join(event_classifications, event_classifications.c.event_id == events.c.id)
        ).all()
    assert len(evs) == 1
    e = evs[0]
    assert (e.category, e.materiality, e.origin, e.trust_tier) == (
        "short_interest_spike",
        RULE.materiality,
        "finra",
        "T1",
    )
    assert e.url == file_url(date(2026, 9, 15)) + "#ACME"
    assert "16.0%" in e.title and "+6.0 pp" in e.title
    # Fetched within the live window: dated by first sight, not the settlement date.
    assert e.published_at == "2026-10-05T01:00:00Z"


def test_crossing_the_level_fires_but_staying_above_does_not(rw_engine: Engine) -> None:
    seed_tickers(rw_engine, [("ACME", "pure_play")])
    seed_shares(rw_engine, "ACME", 100_000_000)
    level = RULE.level_pct
    below, above, still = int((level - 1) * 1e6), int((level + 1) * 1e6), int((level + 2) * 1e6)
    files = {
        file_url(date(2026, 8, 14)): acme_file("2026-08-14", below),
        file_url(date(2026, 8, 31)): acme_file("2026-08-31", above),  # crosses: +2 pp
        file_url(date(2026, 9, 15)): acme_file("2026-09-15", still),  # stays above: +1 pp
    }
    run(rw_engine, files)
    with rw_engine.connect() as conn:
        urls = conn.execute(select(events.c.url, events.c.published_at, events.c.raw)).all()
    assert [u.url.rsplit("shrt", 1)[1] for u in urls] == ["20260831.csv#ACME"]
    # 2026-08-31 was fetched 35 days after settlement: a backfill row, dated by settlement.
    assert urls[0].published_at == "2026-08-31T00:00:00Z" and "settlement_date" in urls[0].raw
    flags = short_interest_flags(rw_engine, RULE, ["ACME"])
    assert len(flags) == 1 and "at or above" in flags[0].detail


def test_no_event_below_thresholds_or_without_shares(rw_engine: Engine) -> None:
    seed_tickers(rw_engine, [("ACME", "pure_play"), ("EXMP", "etf")])
    seed_shares(rw_engine, "ACME", 100_000_000)
    files = {
        file_url(date(2026, 8, 31)): acme_file("2026-08-31", 10_000_000),
        file_url(date(2026, 9, 15)): acme_file("2026-09-15", 14_000_000),  # +4 pp < 5
    }
    run(rw_engine, files)
    with rw_engine.connect() as conn:
        assert conn.execute(select(events)).all() == []
    assert short_interest_flags(rw_engine, RULE, ["ACME"]) == []


def test_fresh_settlement_dates_are_not_probed(rw_engine: Engine) -> None:
    seed_tickers(rw_engine, [("ACME", "pure_play")])
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(403)

    p = FinraShortInterest(client=httpx.Client(transport=httpx.MockTransport(handler)))
    rule = ShortInterestRule.model_validate({**RULE.model_dump(), "months_back": 1})
    ingest_short_interest(rw_engine, p, rule, today=date(2026, 10, 3))
    assert not any("20260930" in u or "20260929" in u for u in seen)  # 3 days old < min_age
    assert any("20260915" in u for u in seen)
