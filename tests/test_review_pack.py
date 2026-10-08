"""M5 acceptance: the monthly review pack. Its Telegram text is plain, ≤4096 chars, has no share
counts, dollar values or account number, and is sent once per month."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, insert, select
from starlette.testclient import TestClient

from aether.config import load_rubric, load_strategies
from aether.db.engine import write_tx
from aether.db.models import alerts, fx_rates, review_packs
from aether.portfolio.holdings import (
    HoldingsUpdate,
    PositionIn,
    SettingsUpdate,
    apply_holdings_update,
    apply_settings_update,
)
from aether.review.pack import fit, monthly_review
from tests.conftest import CONFIG_DIR
from tests.holdings_data import add_filing, fake_run, seed_prices

CONFIG = load_strategies(CONFIG_DIR)
FLAGS = load_rubric(CONFIG_DIR).risk_flags
NOW = datetime(2026, 10, 1, 2, 30, tzinfo=UTC)
SHARES = Decimal("123.456")
CASH = Decimal("98765.43")


@pytest.fixture
def ready(rw_engine: Engine) -> Engine:
    days = seed_prices(rw_engine)
    apply_holdings_update(
        rw_engine,
        HoldingsUpdate(
            positions=(
                PositionIn(symbol="QTUM", shares=SHARES),
                PositionIn(symbol="ACME", shares=Decimal(77)),
            ),
            cash=CASH,
        ),
    )
    apply_settings_update(rw_engine, SettingsUpdate(whole_shares=False))
    add_filing(
        rw_engine,
        "DEMO",
        "10-Q",
        "2026-08-14",
        parsed={"going_concern": True},
        event=("RISK", "going_concern", 5, "edgar_going_concern_text"),
    )
    fake_run(
        rw_engine, days[-1], {"safe": {"QTUM": 0.75, "ACME": 0.10, "DEMO": 0.10, "EXMP": 0.05}}
    )
    with write_tx(rw_engine) as conn:
        conn.execute(
            insert(fx_rates).values(
                pair="USDSGD", d="2026-09-30", rate=1.29, provider="synthetic", fetched_at="x"
            )
        )
    return rw_engine


def run(engine: Engine, d: date = date(2026, 10, 1)) -> None:
    monthly_review(engine, CONFIG, FLAGS, today=d, telegram=True, now=NOW)


def test_pack_built_and_sent_once_per_month(ready: Engine) -> None:
    run(ready)
    run(ready, date(2026, 10, 2))  # the retry day: already done, nothing new
    run(ready)
    with ready.connect() as conn:
        packs = conn.execute(select(review_packs)).all()
        sent = conn.execute(select(alerts).where(alerts.c.kind == "review_pack")).all()
    assert len(packs) == 1 and packs[0].status == "done"
    assert len(sent) == 1 and sent[0].dedupe_key == "review_pack:2026-10"
    assert sent[0].status == "pending" and sent[0].channel == "telegram"


def test_telegram_text_has_no_holdings_values(ready: Engine) -> None:
    run(ready)
    with ready.connect() as conn:
        text = conn.execute(select(review_packs.c.telegram_text)).scalar_one()
        payload = conn.execute(select(review_packs.c.payload)).scalar_one()
    assert len(text) <= 4096 and "<" not in text
    assert "$" not in text and "S$" not in text
    for secret in (str(SHARES), "123.45", str(CASH), "98765", "98,765", "77 "):
        assert secret not in text
    assert "going concern (event #" in text  # the adjustment chain is cited
    assert re.search(r"Suggested trades: \d+", text)
    # DEMO's freed 10%: EXMP fills to its 10% cap (+5%), ACME is already capped, 5% -> QTUM.
    assert "- QTUM 80.0% (base 75.0% → +5.0% redistributed → 80.0%)" in text
    assert '"sgd"' in payload  # the dashboard pack does carry USD/SGD values


def test_failed_publish_is_recorded_and_retried(rw_engine: Engine) -> None:
    with pytest.raises(RuntimeError):
        run(rw_engine)  # no strategy run yet
    with rw_engine.connect() as conn:
        assert conn.execute(select(review_packs.c.status)).scalar_one() == "failed"
        assert conn.execute(select(func.count()).select_from(alerts)).scalar() == 0


def test_fit_truncates_with_pointer() -> None:
    text = fit([f"line {i} " + "x" * 80 for i in range(200)])
    assert len(text) <= 4096 and text.endswith("more lines on /review")


def test_review_page(ready: Engine, client: TestClient) -> None:
    assert "No review pack yet" in client.get("/review").text
    run(ready)
    page = client.get("/review").text
    assert "2026-10" in page and "going concern" in page and "S$" in page
    assert "<style" not in page and " style=" not in page


def test_m14_thesis_check_and_cap_in_the_pack(ready: Engine) -> None:
    """M14 (spec §6.9): the thesis check and the name-cap status. The Telegram text uses the
    published targets only; the holdings breakdown stays in the dashboard pack."""
    monthly_review(
        ready, CONFIG, FLAGS, today=date(2026, 10, 1), telegram=True, now=NOW, config_dir=CONFIG_DIR
    )
    with ready.connect() as conn:
        text = conn.execute(select(review_packs.c.telegram_text)).scalar_one()
        payload = json.loads(conn.execute(select(review_packs.c.payload)).scalar_one())
    assert "Thesis check:" in text and "Names:" in text and "of 9 used (QTUM excluded)." in text
    assert "Holdings by modality" not in text and "safe targets by modality/sector" in text
    assert "QTUM hyperscaler weight" not in text or "limit 10%" in text
    th = payload["thesis"]
    assert th["report"]["holdings"] is not None  # dashboard only
    assert len(text) <= 4096 and "$" not in text


def test_m14_universe_lines() -> None:
    from aether.review.pack import universe_lines

    u = {
        "status": "done",
        "proposals": [
            {"symbol": "ACME", "action": "watch", "name": "ACME Corp", "track": "pure_play"},
            {
                "symbol": "CLCK",
                "action": "add",
                "name": "CLCK Corp",
                "track": "adjacent",
                "sector": "sensing_timing",
            },
        ],
        "adjacent_status": "done",
        "shortlist": ["CLCK"],
        "slots": {"cap": 9, "active": 8},
        "strong": [],
    }
    lines = universe_lines(u)
    assert "- No changes proposed." in lines  # pure-play track
    assert "Adjacent industries:" in lines and "- add CLCK (sensing timing)" in lines
    assert "- shortlist: CLCK" in lines and "Names: 8 of 9 used." in lines
