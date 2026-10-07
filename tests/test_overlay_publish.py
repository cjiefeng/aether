"""M5 acceptance: monthly targets with the research overlay (layer 1 filing rules).

- a synthetic going-concern 10-Q or 8-K Item 3.01 on a held name → weight 0 at the next publish,
  cited by event ID, freed weight redistributed within the sleeve up to caps, remainder to QTUM;
- a synthetic materiality-5 RISK event → one off-cycle review alert and no automatic change;
- targets change only on a monthly publish or a "Publish targets now" command.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select

from aether.alerts.dispatch import run_alerts
from aether.config import load_alerts_config, load_rubric, load_strategies
from aether.db.models import alerts, profile_targets
from aether.portfolio.holdings import HoldingsUpdate, PositionIn, apply_holdings_update
from aether.portfolio.overlay import CORE, Finding, apply_overlay, chain_text, layer1_findings
from aether.portfolio.publish import next_publish, publish_targets, run_rebalance
from tests.conftest import CONFIG_DIR
from tests.holdings_data import add_event, add_filing, fake_run, seed_prices

CONFIG = load_strategies(CONFIG_DIR)
SLEEVE = ("ACME", "DEMO", "EXMP", "FAKE")
# Medium-style base: QTUM 45%, sleeve 55% (cap 20%).
BASE = {"QTUM": 0.45, "ACME": 0.20, "DEMO": 0.15, "EXMP": 0.10, "FAKE": 0.10}
TOL = 1e-9


def f(symbol: str, rule: str = "going_concern", eid: int | None = 7) -> Finding:
    return Finding(symbol, rule, 0.0, "0009999999-26-000001", "10-Q", "2026-08-01", "u", eid)


def targets(engine: Engine, profile: str = "safe") -> dict[str, dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(profile_targets).where(profile_targets.c.profile == profile)
        ).all()
    return {
        r.as_of: {
            "w": json.loads(r.published_weights),
            "adj": json.loads(r.adjustments),
            "trigger": r.trigger,
            "event": r.trigger_event_id,
        }
        for r in rows
    }


# --------------------------------------------------------------------------- pure overlay


def test_zeroed_name_redistributed_within_sleeve_then_qtum() -> None:
    pub, chain = apply_overlay(BASE, SLEEVE, 0.20, [f("DEMO")])
    assert "DEMO" not in pub
    assert sum(pub.values()) == pytest.approx(1.0)
    # 15% freed, pro rata to EXMP/FAKE (ACME is already at the 20% cap): +7.5% each, under cap.
    assert pub["ACME"] == pytest.approx(0.20)
    assert pub["EXMP"] == pytest.approx(0.175) and pub["FAKE"] == pytest.approx(0.175)
    assert pub[CORE] == pytest.approx(0.45)
    demo = next(c for c in chain if c["symbol"] == "DEMO")
    assert demo["steps"][0]["event_id"] == 7 and demo["final"] == 0
    assert chain_text(demo) == "base 15.0% → going concern (event #7) → 0.0% → 0.0%"


def test_remainder_beyond_caps_goes_to_qtum() -> None:
    pub, chain = apply_overlay(BASE, SLEEVE, 0.20, [f("DEMO"), f("EXMP", "compliance_notice")])
    # 25% freed; FAKE can take only 10% more (to the 20% cap); ACME is full; 15% → QTUM.
    assert pub["FAKE"] == pytest.approx(0.20) and pub["ACME"] == pytest.approx(0.20)
    assert pub[CORE] == pytest.approx(0.60)
    qtum = next(c for c in chain if c["symbol"] == CORE)
    assert qtum["redistributed"] == pytest.approx(0.15)


def test_no_findings_is_identity() -> None:
    pub, chain = apply_overlay(BASE, SLEEVE, 0.20, [])
    assert pub == pytest.approx(BASE)
    assert all(not c["steps"] and c["redistributed"] == 0 for c in chain)


# --------------------------------------------------------------------------- findings


def _held(engine: Engine, sym: str = "DEMO") -> None:
    apply_holdings_update(
        engine,
        HoldingsUpdate(positions=(PositionIn(symbol=sym, shares=Decimal(10)),), cash=Decimal(0)),
    )


GC = ("RISK", "going_concern", 5, "edgar_going_concern_text")
N301 = ("RISK", "delisting_or_compliance", 4, "edgar_8k_3_01")
F25 = ("RISK", "delisting_or_compliance", 3, "edgar_delisting_deregistration")


def _stop_trading(engine: Engine, symbol: str, last: str) -> None:
    from sqlalchemy import delete

    from aether.db.engine import write_tx
    from aether.db.models import prices_daily

    with write_tx(engine) as conn:
        conn.execute(
            delete(prices_daily).where(prices_daily.c.symbol == symbol, prices_daily.c.d > last)
        )


@pytest.mark.parametrize(
    ("form", "kw", "rule", "stops"),
    [
        ("10-Q", {"parsed": {"going_concern": True}, "event": GC}, "going_concern", False),
        (
            "8-K",
            {"items": ["3.01"], "parsed": {"listing_notice": "deficiency"}, "event": N301},
            "compliance_notice",
            False,
        ),
        ("25", {"parsed": {"covers_common": True}, "event": F25}, "acquired_or_delisted", True),
    ],
)
def test_hard_rule_on_held_name_zeroes_it_at_next_publish(
    rw_engine: Engine, form: str, kw: dict, rule: str, stops: bool
) -> None:
    days = seed_prices(rw_engine)
    _held(rw_engine)
    base = {"QTUM": 0.75, "ACME": 0.0625, "DEMO": 0.0625, "EXMP": 0.0625, "FAKE": 0.0625}
    fake_run(rw_engine, days[-1], {"safe": base})
    publish_targets(rw_engine, CONFIG, today=date(2026, 9, 1))
    assert targets(rw_engine)["2026-09-01"]["w"]["DEMO"] == pytest.approx(0.0625)
    _, eid = add_filing(rw_engine, "DEMO", form, "2026-09-15", **kw)
    if stops:
        _stop_trading(rw_engine, "DEMO", "2026-09-01")  # no close since: > 10 QTUM sessions
    # Nothing moves until the next publish.
    run_rebalance(rw_engine, CONFIG)
    assert set(targets(rw_engine)) == {"2026-09-01"}
    publish_targets(rw_engine, CONFIG, today=date(2026, 10, 1))
    row = targets(rw_engine)["2026-10-01"]
    assert "DEMO" not in row["w"]
    assert sum(row["w"].values()) == pytest.approx(1.0)
    for s in ("ACME", "EXMP", "FAKE"):  # redistributed within the sleeve (cap 10%)
        assert row["w"][s] == pytest.approx(0.0625 + 0.0625 / 3)
    assert row["w"]["QTUM"] == pytest.approx(0.75)
    demo = next(c for c in row["adj"]["chain"] if c["symbol"] == "DEMO")
    assert demo["steps"][0]["rule"] == rule and demo["steps"][0]["event_id"] == eid


def test_compliance_notice_expires_and_cleared_accession_is_ignored(rw_engine: Engine) -> None:
    seed_prices(rw_engine)
    acc, _ = add_filing(
        rw_engine,
        "ACME",
        "8-K",
        "2026-01-10",
        items=["3.01"],
        parsed={"listing_notice": "deficiency"},
        event=N301,
    )
    p = CONFIG.overlay
    with rw_engine.connect() as conn:
        assert layer1_findings(conn, SLEEVE, date(2026, 3, 1), p)
        assert not layer1_findings(conn, SLEEVE, date(2026, 9, 1), p)  # > 180 days later
        cleared = p.model_copy(update={"cleared_accessions": (acc,)})
        assert not layer1_findings(conn, SLEEVE, date(2026, 3, 1), cleared)
        assert not layer1_findings(
            conn, SLEEVE, date(2026, 3, 1), p.model_copy(update={"enabled": False})
        )


def test_quarantined_finding_and_older_going_concern_ignored(rw_engine: Engine) -> None:
    seed_prices(rw_engine)
    add_filing(
        rw_engine,
        "ACME",
        "8-K",
        "2026-02-01",
        items=["3.01"],
        parsed={"listing_notice": "deficiency"},
        event=N301,
        quarantined=True,  # would zero ACME if it weren't quarantined
    )
    add_filing(rw_engine, "DEMO", "10-K", "2026-02-01", parsed={"going_concern": True})
    add_filing(rw_engine, "DEMO", "10-Q", "2026-05-01", parsed={"going_concern": False})
    with rw_engine.connect() as conn:
        assert layer1_findings(conn, SLEEVE, date(2026, 6, 1), CONFIG.overlay) == []


@pytest.mark.parametrize(
    ("form", "kw"),
    [
        # Voluntary exchange transfer (8-K 3.01) and an unparsed one: no zero.
        ("8-K", {"items": ["3.01"], "parsed": {"listing_notice": "transfer"}}),
        ("8-K", {"items": ["3.01"], "parsed": {"listing_notice": "ambiguous"}}),
        ("8-K", {"items": ["3.01"]}),
        # Warrant delisting (not the common stock).
        ("25-NSE", {"parsed": {"covers_common": False}}),
        # Common-stock Form 25 for an exchange transfer: the stock keeps trading.
        ("25", {"parsed": {"covers_common": True}}),
        # Change in control (e.g. a de-SPAC close): alert only.
        ("8-K", {"items": ["5.01"]}),
    ],
)
def test_listing_events_that_are_not_permanent_loss_never_zero(
    rw_engine: Engine, form: str, kw: dict
) -> None:
    seed_prices(rw_engine)
    add_filing(rw_engine, "ACME", form, "2026-06-01", **kw)
    with rw_engine.connect() as conn:
        assert layer1_findings(conn, SLEEVE, date(2026, 7, 1), CONFIG.overlay) == []


def test_real_listing_filings_are_classified_without_false_positives() -> None:
    """Recorded EDGAR documents (2026-10-05) that the plain form/item rules would have read as
    permanent loss: IONQ/QBTS warrant 25-NSEs, QBTS's and Churchill X's (INFQ) 3.01 transfer
    notices, and the two common-stock Form 25s filed for those exchange transfers."""
    import gzip

    from aether.edgar.submissions import FilingMeta
    from aether.edgar.text import extract_delisted_class, extract_listing_notice, html_to_text
    from aether.ingest.edgar import parse_document, wants_document
    from tests.conftest import REPO

    docs = json.loads(
        gzip.decompress(
            (REPO / "tests/fixtures/edgar/listing_docs_2026-10-05.json.gz").read_bytes()
        )
    )
    got = {}
    for acc, d in docs.items():
        meta = FilingMeta(
            acc,
            d["cik"],
            d["form"],
            "2026-01-01",
            None,
            None,
            tuple(d["items"]),
            d["primary_doc"],
            None,
            False,
        )
        assert wants_document(meta)  # the ingest fetches and parses it
        summary = parse_document(d["symbol"], meta, d["body"]).summary
        assert (
            ("listing_notice" in summary)
            if d["form"].startswith("8-K")
            else ("covers_common" in summary)
        )
        if d["form"].startswith("8-K"):
            got[(d["symbol"], d["form"])] = extract_listing_notice(html_to_text(d["body"])).kind
        else:
            c = extract_delisted_class(d["body"])
            got[(d["symbol"], d["form"])] = c.covers_common if c else None
    assert got == {
        ("IONQ", "25-NSE"): False,  # redeemable warrants
        ("QBTS", "25-NSE"): False,  # warrants
        ("QBTS", "8-K"): "transfer",  # NYSE -> Nasdaq
        ("INFQ", "8-K"): "transfer",  # Nasdaq -> NYSE with the business combination
        ("QBTS", "25"): True,  # common stock, but it kept trading (transfer)
        ("INFQ", "25"): True,  # SPAC shares, kept trading as INFQ
    }


# --------------------------------------------------------------------------- publish cadence


def test_targets_change_only_on_publish(rw_engine: Engine) -> None:
    days = seed_prices(rw_engine)
    fake_run(rw_engine, days[-3], {"safe": {"QTUM": 0.75, "ACME": 0.25}})
    run_rebalance(rw_engine, CONFIG, today=date(2026, 9, 1))  # bootstrap publish
    assert list(targets(rw_engine)) == ["2026-09-01"]
    # New backtests daily: the plan refreshes, the targets don't.
    for d in days[-2:]:
        fake_run(rw_engine, d, {"safe": {"QTUM": 0.75, "DEMO": 0.25}}, tag=d)
        run_rebalance(rw_engine, CONFIG, today=date(2026, 9, 2))
    assert list(targets(rw_engine)) == ["2026-09-01"]
    assert targets(rw_engine)["2026-09-01"]["w"] == {"ACME": 0.25, "QTUM": 0.75}
    # "Publish targets now" (off-cycle) picks up the latest backtest and cites the event.
    eid = add_event(rw_engine, "DEMO", "2026-09-02T14:00:00Z")
    publish_targets(
        rw_engine, CONFIG, today=date(2026, 9, 3), trigger="off_cycle", trigger_event_id=eid
    )
    row = targets(rw_engine)["2026-09-03"]
    assert row["w"] == {"DEMO": 0.25, "QTUM": 0.75}
    assert row["trigger"] == "off_cycle" and row["event"] == eid


def test_config_change_republishes_with_its_own_trigger(rw_engine: Engine) -> None:
    """Issue #13: a backtest under a new `strategies.yaml` republishes at once (no waiting for
    the 1st); the next data-only backtest doesn't."""
    days = seed_prices(rw_engine)
    old_cfg = json.dumps({"profiles": {"safe": {"qtum_weight": 0.5}}})
    new_cfg = json.dumps({"profiles": {"safe": {"qtum_weight": 0.75}}})
    fake_run(rw_engine, days[-3], {"safe": {"QTUM": 0.5, "ACME": 0.5}}, config=old_cfg)
    run_rebalance(rw_engine, CONFIG, today=date(2026, 9, 1))  # bootstrap publish
    fake_run(rw_engine, days[-2], {"safe": {"QTUM": 0.75, "ACME": 0.25}}, tag="n", config=new_cfg)
    run_rebalance(rw_engine, CONFIG, today=date(2026, 9, 8))
    row = targets(rw_engine)["2026-09-08"]
    assert row["w"] == {"ACME": 0.25, "QTUM": 0.75}
    assert row["trigger"] == "config_change" and row["event"] is None
    # Same config, new prices: frozen until the 1st again.
    fake_run(rw_engine, days[-1], {"safe": {"QTUM": 0.75, "DEMO": 0.25}}, tag="d", config=new_cfg)
    run_rebalance(rw_engine, CONFIG, today=date(2026, 9, 9))
    assert list(targets(rw_engine)) == ["2026-09-01", "2026-09-08"]
    assert targets(rw_engine)["2026-09-08"]["w"] == {"ACME": 0.25, "QTUM": 0.75}


def test_next_publish() -> None:
    assert next_publish(date(2026, 9, 1)) == date(2026, 10, 1)
    assert next_publish(date(2026, 12, 15)) == date(2027, 1, 1)


def test_no_qualifying_strategy_keeps_previous(rw_engine: Engine) -> None:
    days = seed_prices(rw_engine)
    fake_run(rw_engine, days[-2], {"safe": {"QTUM": 0.75, "ACME": 0.25}})
    publish_targets(rw_engine, CONFIG, today=date(2026, 9, 1))
    fake_run(rw_engine, days[-1], {"safe": None, "medium": {"QTUM": 0.45, "ACME": 0.55}})
    publish_targets(rw_engine, CONFIG, today=date(2026, 10, 1))
    row = targets(rw_engine)["2026-10-01"]
    assert row["w"] == {"ACME": 0.25, "QTUM": 0.75}
    assert "previous targets kept" in row["adj"]["note"]


# --------------------------------------------------------------------------- off-cycle alert


def test_materiality_5_event_sends_one_off_cycle_alert_and_changes_nothing(
    rw_engine: Engine,
) -> None:
    days = seed_prices(rw_engine)
    _held(rw_engine, "FAKE")
    fake_run(rw_engine, days[-1], {"safe": {"QTUM": 0.75, "FAKE": 0.25}})
    publish_targets(rw_engine, CONFIG, today=date(2026, 9, 1))
    before = targets(rw_engine)
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    eid = add_event(rw_engine, "FAKE", "2026-09-10T02:00:00Z", materiality=5)
    cfg = load_alerts_config(CONFIG_DIR)
    flags = load_rubric(CONFIG_DIR).risk_flags
    for _ in range(3):
        run_alerts(rw_engine, cfg, flags, None, "off", now=now, off_cycle_min_materiality=4)
        run_rebalance(rw_engine, CONFIG, today=date(2026, 9, 10))
    with rw_engine.connect() as conn:
        rows = conn.execute(select(alerts).where(alerts.c.kind == "off_cycle_review")).all()
    assert len(rows) == 1 and rows[0].event_id == eid
    assert "Publish targets now" in rows[0].text
    assert targets(rw_engine) == before  # no automatic target change
    # Materiality 3 and quarantined events don't suggest a review.
    add_event(rw_engine, "FAKE", "2026-09-10T03:00:00Z", materiality=3)
    add_event(rw_engine, "FAKE", "2026-09-10T04:00:00Z", materiality=5, quarantined=True)
    run_alerts(rw_engine, cfg, flags, None, "off", now=now, off_cycle_min_materiality=4)
    with rw_engine.connect() as conn:
        n = conn.execute(
            select(func.count()).select_from(alerts).where(alerts.c.kind == "off_cycle_review")
        ).scalar()
    assert n == 1
