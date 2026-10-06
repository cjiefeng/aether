"""M10: the weekly brief (deterministic) and the review pack's M10 sections. The Telegram copies
are plain, ≤4096 chars, carry no dollar values or share counts, and are sent once."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Engine, select

from aether.config import load_rubric, load_strategies, load_weights
from aether.db.models import alerts, briefs, review_packs
from aether.portfolio.holdings import HoldingsUpdate, PositionIn, apply_holdings_update
from aether.review.pack import monthly_review
from aether.synthesize.brief import telegram_text, week_key, weekly_brief
from tests.conclusions_data import add_conclusion
from tests.conftest import CONFIG_DIR
from tests.holdings_data import add_event, fake_run, seed_prices

W = load_weights(CONFIG_DIR)
RUBRIC = load_rubric(CONFIG_DIR)
SUNDAY = date(2026, 10, 4)
NOW = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)
SHARES = Decimal("4321.5")


def populate(engine: Engine) -> None:
    days = seed_prices(engine)
    apply_holdings_update(
        engine,
        HoldingsUpdate(positions=(PositionIn(symbol="ACME", shares=SHARES),), cash=Decimal(5000)),
    )
    fake_run(engine, days[-1], {"safe": {"QTUM": 0.75, "ACME": 0.10, "DEMO": 0.15}})
    first = add_conclusion(engine, "ACME", "2026-09-20", "HOLD")
    add_conclusion(engine, "ACME", "2026-10-04", "AVOID", prev_id=first)
    add_conclusion(engine, "DEMO", "2026-10-04", "HOLD", held=True, proposed="ACCUMULATE")
    add_conclusion(engine, None, "2026-10-04", "NEUTRAL")
    add_event(engine, "ACME", "2026-10-01T12:00:00Z", 4, "SIGNAL", "contract_with_value")
    add_event(engine, "DEMO", "2026-10-02T12:00:00Z", 3, "RISK", "dilution")
    add_event(engine, "DEMO", "2026-10-02T13:00:00Z", 1, "NOISE", "analyst_rating")


def test_week_key() -> None:
    assert week_key(SUNDAY) == "2026-W40"


def test_brief_is_built_and_sent_once(rw_engine: Engine) -> None:
    populate(rw_engine)
    from aether.portfolio.publish import run_rebalance

    run_rebalance(rw_engine, load_strategies(CONFIG_DIR), today=SUNDAY, weights=W)
    for _ in range(2):
        weekly_brief(
            rw_engine, W.track_record, RUBRIC.risk_flags, today=SUNDAY, telegram=True, now=NOW
        )
    with rw_engine.connect() as conn:
        (b,) = conn.execute(select(briefs)).all()
        sent = conn.execute(select(alerts).where(alerts.c.kind == "weekly_brief")).all()
    assert b.status == "done" and b.week == "2026-W40" and len(sent) == 1
    text = sent[0].text
    assert text == b.telegram_text and len(text) <= 4096
    assert "$" not in text and str(SHARES) not in text and "4321" not in text
    assert "ACME: HOLD → AVOID" in text
    assert "DEMO: proposed ACCUMULATE, held at HOLD" in text
    assert "Theme tilt: NEUTRAL" in text and "No track record yet." in text
    assert "Top signals:" in text and "contract_with_value" in text
    assert "Risks:" in text and "dilution" in text
    assert "Filtered noise: 1 NOISE items" in text
    assert re.search(r"Position drift: \d+ names outside the no-trade band", text)
    payload = json.loads(b.payload)
    assert payload["drift"]["profile"] == "safe"  # drift lines stay on the dashboard


def test_brief_text_fits_telegram(rw_engine: Engine) -> None:
    populate(rw_engine)
    from aether.synthesize.brief import build_brief

    b = build_brief(rw_engine, SUNDAY, W.track_record, RUBRIC.risk_flags)
    b["signals"] = b["signals"] * 400
    text = telegram_text(b)
    assert len(text) <= 4096 and text.endswith("more lines on /briefs")


def test_review_pack_has_stances_and_overlay_line(rw_engine: Engine) -> None:
    populate(rw_engine)
    monthly_review(
        rw_engine,
        load_strategies(CONFIG_DIR),
        RUBRIC.risk_flags,
        today=date(2026, 10, 1),
        telegram=True,
        now=NOW,
        weights=W,
    )
    with rw_engine.connect() as conn:
        pack = conn.execute(select(review_packs)).one()
    p = json.loads(pack.payload)
    assert {s["key"] for s in p["stances"]} >= {"ACME", "DEMO", "$THEME"}
    assert "Stances (track record):" in pack.telegram_text
    assert "Overlay value-added: no publish has a 1-month result yet." in pack.telegram_text
    assert "$" not in pack.telegram_text.replace("$THEME", "")
