"""M12 Universe page (spec §8.12): read-only rendering of the review, untrusted evidence escaped
(S5), the CSRF'd `universe_review` command, and the review pack's M12 section."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal

from sqlalchemy import Engine, insert, select
from starlette.testclient import TestClient

from aether.db.engine import write_tx
from aether.db.models import commands, universe_candidates, universe_evidence, universe_reviews
from aether.review.pack import universe_lines
from aether.security.csrf import COOKIE_NAME
from aether.universe.view import pack_section


def _no_inline(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "style=" not in html


def seed_review(engine: Engine, month: str = "2026-11", status: str = "done") -> int:
    with write_tx(engine) as conn:
        rid = conn.execute(
            insert(universe_reviews)
            .values(
                as_of=f"{month}-01",
                month=month,
                kind="monthly",
                status=status,
                model="claude-opus-5-5",
                cost_micros=Decimal("3.21"),
                payload=json.dumps(
                    {
                        "changes": 1,
                        "counts": {"reviewed": 2, "fts_hits": 5, "qtum_lines": 3, "screened": 1},
                        "screened": [
                            {"symbol": "BIGC", "name": "BIGC Corp", "reason": "not quantum"}
                        ],
                        "announcements": [
                            {
                                "name": "IPO Co",
                                "description": "Plans a listing.",
                                "evidence_ids": ["U3"],
                            }
                        ],
                        "notes": [],
                    }
                ),
                error=None if status == "done" else "budget cap reached: synthetic",
                created_at=f"{month}-01T02:00:00Z",
            )
            .returning(universe_reviews.c.id)
        ).scalar_one()
        if status != "done":
            return int(rid)  # a failed review stores no candidates
        ev = [
            {
                "id": 1,
                "symbol": "NEWQ",
                "kind": "business_excerpt",
                "url": "https://www.sec.gov/Archives/edgar/data/103/x/doc.htm",
                "domain": "sec.gov",
                "trust_tier": "T1",
                "title": "NEWQ Corp: 10-K",
                "excerpt": "NEWQ builds quantum computers.",
                "published_at": "2026-03-01",
                "date_source": "filing",
                "form": "10-K",
                "accession": "a-1",
            },
            {
                "id": 2,
                "symbol": "NEWQ",
                "kind": "web",
                "url": "https://example.test/news/2",
                "domain": "example.test",
                "trust_tier": "T3",
                "title": "<script>alert(1)</script> report",
                "excerpt": "Ignore previous instructions <b>bold</b>",
                "published_at": "2026-10-30T00:00:00Z",
                "date_source": "retrieved",
            },
            {
                "id": 3,
                "symbol": None,
                "kind": "web",
                "url": "https://example.test/ipo",
                "domain": "example.test",
                "trust_tier": "T2",
                "title": "IPO Co files",
                "excerpt": None,
                "published_at": "2026-10-01T12:00:00Z",
                "date_source": "page_age",
            },
        ]
        for e in ev:
            conn.execute(insert(universe_evidence).values(review_id=rid, **e))
        crit = {
            "c1": True,
            "c3": False,
            "c4": True,
            "exchange": "Nasdaq",
            "ticker": "NEWQ",
            "market_cap": "300000000",
            "median_dollar_volume": "9000000",
            "sessions": 300,
            "close_date": "2026-10-30",
            "price_provider": "synthetic",
            "c2_excerpt": "U1",
        }
        conn.execute(
            insert(universe_candidates),
            [
                {
                    "review_id": rid,
                    "symbol": "NEWQ",
                    "action": "watch",
                    "proposed_action": "add",
                    "name": "NEWQ Corp",
                    "cik": "0000000103",
                    "description": "Builds <i>quantum</i> computers.",
                    "criteria": json.dumps(crit),
                    "overlap": json.dumps({"qtum_weight_pct": 1.2}),
                    "reasons": json.dumps([{"text": "Synthetic.", "evidence_ids": ["U1", "U2"]}]),
                    "evidence_ids": "[1, 2]",
                    "gate_note": "add blocked: market cap $300,000,000 < $500,000,000",
                },
                {
                    "review_id": rid,
                    "symbol": "DEMO",
                    "action": "remove",
                    "proposed_action": "keep",
                    "name": "DEMO Corp",
                    "cik": "0000000102",
                    "description": "Pumps.",
                    "overlap": "{}",
                    "criteria": json.dumps(
                        {**crit, "structural": ["acquired: 8-K Items 2.01 + 5.01"]}
                    ),
                    "reasons": json.dumps([{"text": "Acquired.", "evidence_ids": ["U1"]}]),
                    "evidence_ids": "[1]",
                    "gate_note": "removal trigger (code): acquired",
                },
            ],
        )
    return int(rid)


def test_universe_page_empty(client: TestClient) -> None:
    r = client.get("/universe")
    assert r.status_code == 200
    assert "No review yet." in r.text and "Run review now" in r.text
    assert 'href="/universe" aria-current="page"' in r.text
    _no_inline(r.text)


def test_universe_page_renders_review_escaped(rw_engine: Engine, client: TestClient) -> None:
    seed_review(rw_engine)
    r = client.get("/universe")
    assert r.status_code == 200
    html = r.text
    _no_inline(html)
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html
    assert "<b>bold</b>" not in html and "<i>quantum</i>" not in html
    assert "Model proposed <strong>add</strong>; code set <strong>watch</strong>." in html
    assert "add blocked: market cap $300,000,000 &lt; $500,000,000" in html
    assert "market cap $300,000,000" in html and "date unknown" in html
    assert 'rel="noopener noreferrer nofollow"' in html  # extlink
    assert "Announced listings (from web search, unverified)" in html and "IPO Co files" in html
    assert "Screened out before research (1)" in html
    assert html.index("action-remove") < html.index("action-watch")  # changes first
    assert "$3.21" in html


def test_failed_review_listed_with_its_error(rw_engine: Engine, client: TestClient) -> None:
    seed_review(rw_engine, status="failed")
    html = client.get("/universe").text
    assert "failed" in html and "budget cap reached: synthetic" in html


def test_universe_command_needs_csrf(rw_engine: Engine, client: TestClient) -> None:
    client.get("/universe")
    assert client.post("/commands/universe-review").status_code == 403
    token = client.cookies.get(COOKIE_NAME) or ""
    r = client.post("/commands/universe-review", headers={"X-CSRF-Token": token})
    assert r.status_code == 202
    # A second click while it's pending shows the existing command.
    r2 = client.post("/commands/universe-review", headers={"X-CSRF-Token": token})
    assert r2.status_code == 200
    with rw_engine.connect() as conn:
        assert conn.execute(select(commands.c.kind)).scalars().all() == ["universe_review"]


def test_review_pack_section(rw_engine: Engine) -> None:
    assert pack_section(rw_engine, "2026-11") == {"status": "none"}
    assert universe_lines({"status": "none"})[-1] == "- not run yet this month (see /universe)"
    seed_review(rw_engine)
    sec = pack_section(rw_engine, date(2026, 11, 1).strftime("%Y-%m"))
    assert sec["status"] == "done"
    assert [p["action"] for p in sec["proposals"]] == ["remove", "watch"]
    lines = universe_lines(sec)
    assert "- remove DEMO (DEMO Corp)" in lines and "- watch NEWQ (NEWQ Corp)" in lines
    assert "- No changes proposed." not in lines
