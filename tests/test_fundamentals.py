"""M9: point-in-time fundamentals (TTM, fully diluted shares, runway, EV) and the overlay's
dilution / runway haircuts (spec §6.1, §6.6.1).

Acceptance: a synthetic 25% YoY rise in fully diluted shares halves the name's weight at the next
publish, cited.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select

from aether.config import load_strategies
from aether.db.models import profile_targets
from aether.portfolio.overlay import chain_text, layer1_findings
from aether.portfolio.publish import publish_targets
from aether.score import fundamentals as fnd
from tests.conftest import CONFIG_DIR
from tests.fundamentals_data import (
    CASH,
    COMMON,
    DEBT,
    OPTIONS,
    REV,
    STI,
    WARRANTS,
    add_facts,
    cash_flow_history,
    fact,
)
from tests.holdings_data import fake_run, seed_prices

CONFIG = load_strategies(CONFIG_DIR)
SLEEVE = ("ACME", "DEMO", "EXMP", "FAKE")
AS_OF = date(2026, 9, 15)  # after every synthetic 10-Q in these tests was filed


def facts_for(rows: list[dict], as_of: date = AS_OF) -> list[fnd.Fact]:
    out = []
    for r in rows:
        if r["filed"] > as_of.isoformat():
            continue
        v = r["value_micros"] if r["value_micros"] is not None else Decimal(r["value_int"])
        out.append(
            fnd.Fact(
                r["concept"],
                date.fromisoformat(r["period_end"]),
                r["period_days"],
                Decimal(v),
                r["accession"],
                r["form"],
                r["filed"],
            )
        )
    return out


# --------------------------------------------------------------------------- TTM


def test_ttm_from_fiscal_year_plus_ytd_minus_prior_ytd() -> None:
    rows = [
        fact("ACME", REV, "2025-03-31", 10, days=90),
        fact("ACME", REV, "2025-06-30", 25, days=181),
        fact("ACME", REV, "2025-12-31", 60, days=365, form="10-K"),
        fact("ACME", REV, "2026-03-31", 20, days=90),
        fact("ACME", REV, "2026-06-30", 45, days=181),
    ]
    t = fnd.latest_ttm(facts_for(rows), fnd.REVENUE)
    assert t is not None and t.end == date(2026, 6, 30) and t.method == "ytd"
    assert t.value == Decimal(60 + 45 - 25)
    assert len(t.refs) == 3


def test_ttm_is_fiscal_year_when_one_ends_at_the_latest_period() -> None:
    rows = [fact("ACME", REV, "2025-12-31", 60, days=365, form="10-K")]
    t = fnd.latest_ttm(facts_for(rows), fnd.REVENUE)
    assert t is not None and t.method == "fy" and t.value == 60


def test_ttm_missing_prior_ytd_is_none_not_guessed() -> None:
    rows = [
        fact("ACME", REV, "2025-12-31", 60, days=365, form="10-K"),
        fact("ACME", REV, "2026-06-30", 45, days=181),  # no 2025-06-30 H1 value
    ]
    t = fnd.latest_ttm(facts_for(rows), fnd.REVENUE)
    # Falls back to the most recent computable TTM (the FY), never an estimate.
    assert t is not None and t.method == "fy" and t.end == date(2025, 12, 31)


def test_facts_filed_after_as_of_are_invisible() -> None:
    rows = [fact("ACME", CASH, "2026-06-30", 100, filed="2026-08-10")]
    assert fnd.latest_instant(facts_for(rows, date(2026, 8, 9)), CASH) is None
    assert fnd.latest_instant(facts_for(rows, date(2026, 8, 10)), CASH) is not None


# --------------------------------------------------------------------------- FD shares


def test_fully_diluted_sums_tagged_components_and_lists_missing() -> None:
    rows = [
        fact("ACME", COMMON, "2026-07-31", 1000),
        fact("ACME", WARRANTS, "2026-06-30", 100),
        fact("ACME", OPTIONS, "2025-12-31", 50),
    ]
    fs = facts_for(rows)
    common = fnd.common_shares(fs)
    assert common is not None
    fd = fnd.fully_diluted_at(fs, common)
    assert fd.total == 1150
    assert set(fd.components) == {"warrants", "options"}
    assert set(fd.missing) == {"rsus", "convertible_shares"}


def test_stale_component_is_not_counted() -> None:
    rows = [fact("ACME", COMMON, "2026-07-31", 1000), fact("ACME", WARRANTS, "2021-09-30", 500)]
    fs = facts_for(rows)
    common = fnd.common_shares(fs)
    assert common is not None
    assert fnd.fully_diluted_at(fs, common).total == 1000


def test_fd_yoy_same_components_both_ends() -> None:
    rows = [
        fact("ACME", COMMON, "2025-07-31", 1000),
        fact("ACME", COMMON, "2026-07-31", 1200),
        fact("ACME", WARRANTS, "2025-06-30", 100),
        fact("ACME", WARRANTS, "2026-06-30", 175),
        fact("ACME", OPTIONS, "2026-06-30", 999),  # only tagged at the end: not compared
    ]
    y = fnd.fd_yoy(facts_for(rows), date(2024, 1, 2))
    assert y["value"] == pytest.approx(1375 / 1100 - 1)
    assert y["components_compared"] == ["common", "warrants"]


def test_fd_yoy_base_before_first_session_is_na() -> None:
    """INFQ-style: the year-ago count belongs to the SPAC, before the stock's first session."""
    rows = [fact("ACME", COMMON, "2025-07-31", 100), fact("ACME", COMMON, "2026-07-31", 900)]
    y = fnd.fd_yoy(facts_for(rows), date(2026, 2, 17))
    assert y["value"] is None and y["reason"].startswith("listed < 1 year")


def test_fd_yoy_without_share_count() -> None:
    y = fnd.fd_yoy([], None)
    assert y["value"] is None and "no company-wide" in y["reason"]


# --------------------------------------------------------------------------- runway / EV


def test_runway_and_ev() -> None:
    rows = [
        *cash_flow_history("ACME", -25),  # TTM OCF -100 → burn 25/quarter
        fact("ACME", CASH, "2026-06-30", 60),
        fact("ACME", STI, "2026-06-30", 40),
        fact("ACME", DEBT, "2026-06-30", 10),
        fact("ACME", COMMON, "2026-07-31", 1000),
        fact("ACME", REV, "2025-12-31", 50, days=365, form="10-K"),
    ]
    s = fnd.compute(facts_for(rows), "ACME", AS_OF, first_session=None, price=("2026-08-31", 2.0))
    assert s.quarterly_burn == Decimal(25)
    assert s.liquidity == Decimal(100)
    assert s.runway_months == pytest.approx(12.0)  # 100 / 25 * 3
    assert s.ev == Decimal(2000 + 10 - 100)
    assert s.ev_sales == pytest.approx(1910 / 50)
    json.dumps(s.to_json())  # serialisable


def test_cash_generative_company_is_not_burning() -> None:
    rows = [*cash_flow_history("ACME", 5), fact("ACME", CASH, "2026-06-30", 60)]
    s = fnd.compute(facts_for(rows), "ACME", AS_OF, first_session=None, price=None)
    assert s.not_burning and s.runway_months is None


def test_no_share_count_means_no_ev_with_reason() -> None:
    rows = [fact("ACME", CASH, "2026-06-30", 60)]
    s = fnd.compute(facts_for(rows), "ACME", AS_OF, first_session=None, price=("d", 1.0))
    assert s.ev is None and "share count" in s.reasons["ev"]


# --------------------------------------------------------------------------- overlay (acceptance)


def _published(engine: Engine, as_of: str, profile: str = "safe") -> tuple[dict, list]:
    with engine.connect() as conn:
        r = conn.execute(
            select(profile_targets).where(
                profile_targets.c.profile == profile, profile_targets.c.as_of == as_of
            )
        ).one()
    return json.loads(r.published_weights), json.loads(r.adjustments)["chain"]


BASE = {"QTUM": 0.75, "ACME": 0.0625, "DEMO": 0.0625, "EXMP": 0.0625, "FAKE": 0.0625}


def test_25pct_fd_rise_halves_weight_at_next_publish_cited(rw_engine: Engine) -> None:
    days = seed_prices(rw_engine, n=450, start=date(2025, 1, 2))
    fake_run(rw_engine, days[-1], {"safe": BASE})
    publish_targets(rw_engine, CONFIG, today=date(2026, 8, 1))
    assert _published(rw_engine, "2026-08-01")[0]["DEMO"] == pytest.approx(0.0625)

    add_facts(
        rw_engine,
        [
            fact("DEMO", COMMON, "2025-07-31", 1_000_000, filed="2025-08-10"),
            fact("DEMO", COMMON, "2026-07-31", 1_250_000, filed="2026-08-10"),
        ],
    )
    publish_targets(rw_engine, CONFIG, today=date(2026, 9, 1))
    w, chain = _published(rw_engine, "2026-09-01")
    assert w["DEMO"] == pytest.approx(0.0625 / 2)
    for s in ("ACME", "EXMP", "FAKE"):  # freed weight redistributed within the sleeve
        assert w[s] == pytest.approx(0.0625 + 0.0625 / 2 / 3)
    assert w["QTUM"] == pytest.approx(0.75)
    demo = next(c for c in chain if c["symbol"] == "DEMO")
    step = demo["steps"][0]
    assert step["rule"] == "fd_dilution" and step["multiplier"] == 0.5
    assert step["accession"].startswith("0009999998-26-")  # cites the filing behind the count
    assert step["detail"]["yoy"] == pytest.approx(0.25)
    assert "FD shares +25.0% YoY" in chain_text(demo)


def test_low_runway_halves_and_both_haircuts_compound(rw_engine: Engine) -> None:
    seed_prices(rw_engine, n=450, start=date(2025, 1, 2))
    add_facts(
        rw_engine,
        [
            *cash_flow_history("EXMP", -100),  # burn 100/quarter
            fact("EXMP", CASH, "2026-06-30", 200),  # 6 months
            fact("FAKE", COMMON, "2025-07-31", 100),
            fact("FAKE", COMMON, "2026-07-31", 130),
            *cash_flow_history("FAKE", -100),
            fact("FAKE", CASH, "2026-06-30", 100),  # 3 months
        ],
    )
    with rw_engine.connect() as conn:
        fs = layer1_findings(conn, SLEEVE, AS_OF, CONFIG.overlay)
    got = sorted((f.symbol, f.rule, f.multiplier) for f in fs)
    assert got == [
        ("EXMP", "low_runway", 0.5),
        ("FAKE", "fd_dilution", 0.5),
        ("FAKE", "low_runway", 0.5),
    ]
    from aether.portfolio.overlay import apply_overlay

    pub, _ = apply_overlay(BASE, SLEEVE, 0.10, fs)
    assert pub["FAKE"] == pytest.approx(0.0625 * 0.25)
    assert pub["EXMP"] == pytest.approx(0.0625 * 0.5)


def test_haircuts_respect_thresholds_listing_date_and_clearing(rw_engine: Engine) -> None:
    seed_prices(rw_engine, n=200, start=date(2026, 1, 2))  # first session 2026-01-02
    add_facts(
        rw_engine,
        [
            # 30% rise, but the base predates the first session (de-SPAC style): no finding.
            fact("ACME", COMMON, "2025-07-31", 100),
            fact("ACME", COMMON, "2026-07-31", 130),
            # Runway exactly 12 months: not below the minimum.
            *cash_flow_history("DEMO", -25),
            fact("DEMO", CASH, "2026-06-30", 100),
        ],
    )
    with rw_engine.connect() as conn:
        assert layer1_findings(conn, SLEEVE, AS_OF, CONFIG.overlay) == []
        # Before the 10-Q was filed (filed = period end + 40 days), nothing is visible either.
        assert layer1_findings(conn, SLEEVE, date(2026, 7, 1), CONFIG.overlay) == []


# --------------------------------------------------------------------------- live-check regressions


def test_liquidity_counts_marketable_securities_once_per_bucket() -> None:
    """Live check (M9): a filer with little cash but large AFS securities, tagged under several
    overlapping concepts, must not read as a short runway."""
    afs_cur = "us-gaap:DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent"
    mkt_cur = "us-gaap:MarketableSecuritiesCurrent"  # same holding, second concept
    afs_non = "us-gaap:DebtSecuritiesAvailableForSaleExcludingAccruedInterestNoncurrent"
    rows = [
        *cash_flow_history("ACME", -15),
        fact("ACME", CASH, "2026-06-30", 28),
        fact("ACME", afs_cur, "2026-06-30", 366),
        fact("ACME", mkt_cur, "2026-06-30", 366),
        fact("ACME", afs_non, "2026-06-30", 148),
        fact("ACME", "us-gaap:LongTermInvestments", "2026-06-30", 999),  # not counted
    ]
    s = fnd.compute(facts_for(rows), "ACME", AS_OF, first_session=None, price=None)
    assert s.liquidity == Decimal(28 + 366 + 148)
    assert s.runway_months == pytest.approx(542 / 15 * 3)


def test_revenue_including_assessed_tax_and_stale_ttm_ignored() -> None:
    inc = "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax"
    rows = [
        fact("ACME", "us-gaap:Revenues", "2022-12-31", 13, days=365, form="10-K"),  # old concept
        fact("ACME", inc, "2025-12-31", 20, days=365, form="10-K"),
    ]
    s = fnd.compute(facts_for(rows), "ACME", AS_OF, first_session=None, price=None)
    assert s.revenue_ttm is not None and s.revenue_ttm.concept == inc
    only_old = [rows[0]]
    s = fnd.compute(facts_for(only_old), "ACME", AS_OF, first_session=None, price=None)
    assert s.revenue_ttm is None and "stale" in s.reasons["revenue"]
