"""M10 dashboard: the conclusion card with citations and the "No track record yet." banner, the
Overview stance columns and tilt banner, /track-record, /briefs, and the CSRF'd `synthesize`
command. Synthetic data only."""

from __future__ import annotations

import json
import re

from sqlalchemy import Engine, select, update
from starlette.testclient import TestClient

from aether.db.engine import write_tx
from aether.db.models import commands, conclusions
from aether.security.csrf import COOKIE_NAME
from tests.conclusions_data import add_conclusion
from tests.conftest import seed_tickers
from tests.holdings_data import add_event


def no_inline(body: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", body)
    assert "<style" not in body and " style=" not in body


def seed(engine: Engine) -> tuple[int, int]:
    seed_tickers(engine, [("ACME", "pure_play"), ("QTUM", "etf")])
    eid = add_event(engine, "ACME", "2026-09-20T12:00:00Z", 4, "SIGNAL", "contract_with_value")
    first = add_conclusion(engine, "ACME", "2026-09-20", "HOLD")
    cid = add_conclusion(
        engine, "ACME", "2026-10-04", "HOLD", held=True, proposed="AVOID", prev_id=first
    )
    payload = {
        "one_line_verdict": "Synthetic <b>verdict</b> for ACME.",
        "thesis": [
            {"point": "Synthetic contract point.", "evidence_ids": [f"E{eid}", "S:dilution"]}
        ],
        "bear_case": [],
        "what_would_change_my_mind": ["A synthetic development."],
        "key_dates": [],
    }
    evidence = {
        f"E{eid}": {
            "type": "event",
            "ref": eid,
            "label": "Synthetic event",
            "url": "https://example.test/e",
        },
        "S:dilution": {"type": "score", "ref": "dilution", "label": "Dilution"},
    }
    with write_tx(engine) as conn:
        conn.execute(
            update(conclusions)
            .where(conclusions.c.id == cid)
            .values(
                payload=json.dumps(payload),
                evidence=json.dumps(evidence),
                hold_reason="cooldown until 2026-10-04",
            )
        )
    add_conclusion(engine, None, "2026-10-04", "PURE_PLAYS")
    return eid, cid


def test_ticker_page_conclusion_card(rw_engine: Engine, client: TestClient) -> None:
    seed_tickers(rw_engine, [("ACME", "pure_play"), ("QTUM", "etf")])
    body = client.get("/t/ACME").text
    assert "No conclusion yet." in body and "No track record yet." in body
    eid, cid = seed(rw_engine)
    body = client.get("/t/ACME").text
    no_inline(body)
    assert f"conclusion #{cid}" in body
    assert "Proposed AVOID, held at HOLD: cooldown until 2026-10-04." in body
    assert "&lt;b&gt;verdict&lt;/b&gt;" in body  # model text is escaped, never markup
    assert f'href="/feed?event={eid}"' in body and 'href="#scorecard"' in body
    assert "No track record yet." in body
    assert 'hx-post="/commands/synthesize"' in body
    assert "History (2)" in body


def test_overview_track_record_and_briefs_pages(rw_engine: Engine, client: TestClient) -> None:
    seed(rw_engine)
    body = client.get("/").text
    no_inline(body)
    assert "Theme tilt" in body and "PURE PLAYS" in body
    assert 'href="/t/ACME#conclusion"' in body
    body = client.get("/track-record").text
    no_inline(body)
    assert "No track record yet." in body and "Does the overlay add value?" in body
    assert "overlapping windows" in body
    body = client.get("/briefs").text
    no_inline(body)
    assert "No briefs yet." in body


def test_feed_event_filter_shows_one_event(rw_engine: Engine, client: TestClient) -> None:
    eid, _ = seed(rw_engine)
    other = add_event(rw_engine, "ACME", "2026-09-21T12:00:00Z", 1, "NOISE", "analyst_rating")
    body = client.get(f"/feed?event={eid}").text
    assert "Synthetic contract_with_value event" in body
    assert "analyst_rating event" not in body
    assert "analyst_rating event" in client.get(f"/feed?event={other}").text


def test_synthesize_command_needs_csrf_and_a_known_symbol(
    rw_engine: Engine, client: TestClient
) -> None:
    seed(rw_engine)
    assert client.post("/commands/synthesize", data={"symbol": "ACME"}).status_code == 403
    client.get("/t/ACME")
    h = {"X-CSRF-Token": client.cookies.get(COOKIE_NAME) or ""}
    assert (
        client.post("/commands/synthesize", data={"symbol": "NOPE"}, headers=h).status_code == 400
    )
    assert (
        client.post("/commands/synthesize", data={"symbol": "ACME"}, headers=h).status_code == 202
    )
    with rw_engine.connect() as conn:
        kind, args = conn.execute(select(commands.c.kind, commands.c.args)).one()
    assert kind == "synthesize" and json.loads(args) == {"symbol": "ACME"}
