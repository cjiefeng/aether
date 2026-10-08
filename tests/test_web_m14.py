"""M14 dashboard: Universe tabs (adjacent track, full re-evaluation), the CSRF'd
`universe_full_review` command and its 7-day refusal, thesis panels on Holdings / Strategies /
ticker pages, and the adjacent names on the Overview. Synthetic data only."""

from __future__ import annotations

import json
import re
from decimal import Decimal

from sqlalchemy import Engine, insert, select
from starlette.testclient import TestClient

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import commands, tickers, universe_candidates, universe_reviews
from aether.jobs import full_review_refusal
from aether.security.csrf import COOKIE_NAME
from aether.universe.full import cooldown_ok
from tests.conftest import make_settings


def _no_inline(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "style=" not in html


def _breakdown(rows: list[tuple[str, str, float]], flags: list[str]) -> dict:
    return {
        "rows": [
            {
                "kind": k,
                "category": c,
                "weight": w,
                "pct_ex_qtum": w / 0.55,
                "pct_whole": w,
                "symbols": ["ACME"],
            }
            for k, c, w in rows
        ],
        "flags": [{"flag": "few_modalities", "text": f} for f in flags],
        "qtum": 0.45,
    }


def _seed(engine: Engine, kind: str = "monthly", created: str = "2026-11-01T02:00:00Z") -> int:
    payload: dict = {
        "changes": 1,
        "counts": {"reviewed": 3},
        "notes": [],
        "adjacent": {
            "status": "done",
            "screened": [
                {
                    "symbol": "HYPR",
                    "sector": "pqc_cyber",
                    "reason": "excluded: hyperscaler / cloud platform",
                }
            ],
            "info": [
                {"name": "Private <b>Labs</b>", "status": "not investable", "description": "d"}
            ],
            "notes": [],
        },
        "slots": {"cap": 9, "active": 9, "removals": 0, "free": 0, "adds": [], "blocked": ["CLCK"]},
        "strong": ["CLCK"],
        "shortlist": ["CLCK"],
    }
    if kind == "full":
        payload["full"] = {
            "profile": "medium",
            "cap": 9,
            "members": [
                {
                    "symbol": "ACME",
                    "decision": "keep",
                    "category": "trapped_ion",
                    "category_label": None,
                    "reasons": [{"text": "keeps the trapped-ion slot", "evidence_ids": ["U1"]}],
                },
            ],
            "drops": [
                {
                    "symbol": "DEMO",
                    "reasons": [{"text": "redundant modality", "evidence_ids": ["U1"]}],
                }
            ],
            "not_chosen": [
                {"symbol": "CLCK", "reason": {"text": "weaker evidence", "evidence_ids": ["U1"]}}
            ],
            "before": _breakdown([("modality", "trapped_ion", 0.3)], []),
            "after": _breakdown([("modality", "trapped_ion", 0.55)], ["1 modality held"]),
            "flags": [{"flag": "few_modalities", "text": "1 modality held"}],
            "weights_note": "Illustrative: medium profile.",
        }
        payload["full_lines"] = ["Proposed set: 1 name(s)."]
    with write_tx(engine) as conn:
        rid = int(
            conn.execute(
                insert(universe_reviews)
                .values(
                    as_of="2026-11-01",
                    month="2026-11",
                    kind=kind,
                    status="done",
                    model="claude-opus-5-5",
                    cost_micros=Decimal("4.00"),
                    payload=json.dumps(payload),
                    created_at=created,
                )
                .returning(universe_reviews.c.id)
            ).scalar_one()
        )
        for row in [
            {
                "symbol": "CLCK",
                "track": "adjacent",
                "action": "watch",
                "proposed_action": "add",
                "name": "CLCK Corp",
                "description": "Atomic clocks <script>alert(1)</script>",
                "criteria": json.dumps(
                    {
                        "c1": True,
                        "c3": True,
                        "c4": True,
                        "evidence_t1": 1,
                        "evidence_t2": 2,
                        "strong": True,
                        "strong_note": "meets every strong-candidate threshold",
                        "fills_gap": True,
                        "close_date": "2026-10-30",
                        "price_provider": "synthetic",
                    }
                ),
                "overlap": json.dumps({"qtum_weight_pct": 1.5}),
                "reasons": json.dumps([{"text": "r", "evidence_ids": ["U1"]}]),
                "evidence_ids": "[]",
                "gate_note": "no free slot: 9 of 9 names used. Adding needs a slot.",
                "sector": "sensing_timing",
                "exposure": "high",
                "market_cap_micros": Decimal("10000000000"),
                "mcap_bucket": "mid",
            },
            {
                "symbol": "ACME",
                "track": "pure_play",
                "action": "keep",
                "name": "ACME Corp",
                "criteria": json.dumps({"c1": True, "c3": True, "c4": True}),
                "overlap": "{}",
                "reasons": json.dumps([{"text": "r", "evidence_ids": ["U1"]}]),
                "evidence_ids": "[]",
            },
        ]:
            conn.execute(insert(universe_candidates).values(review_id=rid, **row))
    return rid


def test_universe_adjacent_tab(rw_engine: Engine, client: TestClient) -> None:
    _seed(rw_engine)
    html = client.get("/universe?tab=adjacent").text
    _no_inline(html)
    assert 'class="tab active" href="/universe?tab=adjacent' in html
    assert "Shortlist (sector priority first): <strong>CLCK</strong>" in html
    assert "$10,000,000,000 · mid" in html and "sensing timing" in html and "(fills a gap)" in html
    assert "1 T1, 2 T2" in html and "QTUM 1.50%" in html
    assert "Private &lt;b&gt;Labs&lt;/b&gt;: <strong>not investable</strong>" in html
    assert "HYPR (pqc cyber): excluded: hyperscaler / cloud platform" in html
    assert "&lt;script&gt;alert(1)" in html and "<script>alert(1)" not in html
    assert (
        "Names: 9 of 9 used; 0 slot(s) free" in html
        and "Strong candidates notified: <strong>CLCK</strong>" in html
    )
    assert (
        "ACME Corp" not in html.split("Adjacent industries</h2>")[1]
    )  # pure-play rows on their tab
    pure = client.get("/universe").text
    assert "ACME" in pure and 'id="uv-CLCK"' not in pure


def test_universe_full_tab(rw_engine: Engine, client: TestClient) -> None:
    assert "No full re-evaluation yet." in client.get("/universe?tab=full").text
    _seed(rw_engine, kind="full")
    html = client.get("/universe?tab=full").text
    _no_inline(html)
    assert "Proposed set (1 of 9)" in html
    assert "The proposed set breaks a concentration flag: 1 modality held." in html
    assert "redundant modality" in html and "action-remove" in html
    assert "Before (current set)" in html and "After (proposed set)" in html
    assert "CLCK: weaker evidence" in html


def test_full_review_command_needs_csrf_and_is_refused_within_7_days(
    rw_engine: Engine, client: TestClient
) -> None:
    from datetime import date

    client.get("/universe")
    assert client.post("/commands/universe-full-review").status_code == 403
    token = client.cookies.get(COOKIE_NAME) or ""
    r = client.post("/commands/universe-full-review", headers={"X-CSRF-Token": token})
    assert r.status_code == 202
    with rw_engine.connect() as conn:
        assert conn.execute(select(commands.c.kind)).scalars().all() == ["universe_full_review"]
    settings = make_settings(rw_engine.url.database)  # type: ignore[arg-type]
    assert full_review_refusal(rw_engine, settings, date(2026, 11, 1)) is None
    _seed(rw_engine, kind="full", created="2026-11-01T02:00:00Z")
    assert full_review_refusal(rw_engine, settings, date(2026, 11, 7)) == (
        "a full re-evaluation already ran in the last 7 days"
    )
    assert full_review_refusal(rw_engine, settings, date(2026, 11, 8)) is None
    assert cooldown_ok(None, date(2026, 11, 1), 7)


def test_thesis_panels_render(rw_engine: Engine, client: TestClient) -> None:
    with write_tx(rw_engine) as conn:
        upsert(
            conn,
            tickers,
            [
                {"symbol": "QTUM", "type": "etf", "active": 1},
                {"symbol": "ACME", "type": "pure_play", "active": 1, "modality": "trapped_ion"},
                {"symbol": "ADJA", "type": "adjacent", "active": 1, "sector": "pqc_cyber"},
            ],
            key_cols=["symbol"],
        )
    for path in ("/holdings", "/strategies"):
        html = client.get(path).text
        _no_inline(html)
        assert "Thesis checks" in html and "2 of 9 names used" in html, path
        assert "never change a weight" in html and "Repeated share issuance" in html
        assert "supplier sectors with no name" in html
    t = client.get("/t/ACME").text
    _no_inline(t)
    assert 'id="thesis"' in t and "Cash runway under ~2 years" in t and "Winner signals" in t
    adj = client.get("/t/ADJA").text
    assert 'id="thesis"' in adj and "Winner signals" not in adj
    assert "Adjacent industries" in client.get("/").text
