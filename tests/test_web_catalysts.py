"""M8 dashboard: the Catalysts page, the mark command (CSRF + validation + worker handler), the
ticker page's catalysts / short-interest / options panels, the review pack's M8 sections, and
the 0009 migration (events rebuilt for the `finra` origin without losing child rows).
Synthetic data only."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.exc import IntegrityError
from starlette.testclient import TestClient

from aether.catalysts.sync import refresh_catalysts
from aether.config import load_catalysts_config
from aether.db import migrate
from aether.db.dialect import upsert
from aether.db.engine import ensure_db_file, make_rw_engine, write_tx
from aether.db.models import catalysts, commands, options_snapshots, short_interest
from aether.facts import load_facts, sync_facts
from aether.jobs import process_commands
from aether.review.pack import options_line, telegram_text
from aether.security.csrf import COOKIE_NAME
from tests.conftest import CONFIG_DIR, seed_tickers

TODAY = date(2026, 10, 5)


def no_inline(body: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", body)
    assert "<style" not in body and " style=" not in body


def seed_repo_catalysts(engine: Engine) -> None:
    """The real seed file (IBM roadmap, QBI Stage C, QNT lock-up) over the real facts."""
    seed_tickers(
        engine, [("QTUM", "etf"), ("IONQ", "pure_play"), ("QNT", "pure_play"), ("IBM", "context")]
    )
    with write_tx(engine) as conn:
        sync_facts(conn, load_facts(CONFIG_DIR))
    refresh_catalysts(engine, load_catalysts_config(CONFIG_DIR), TODAY)


def test_catalysts_page_lists_seeds_with_fact_badges(rw_engine: Engine, client: TestClient) -> None:
    seed_repo_catalysts(rw_engine)
    r = client.get("/catalysts")
    assert r.status_code == 200
    body = r.text
    no_inline(body)
    assert "IBM Kookaburra (roadmap year 2026)" in body
    assert "QNT IPO lock-up ends" in body and "2026-11-30" in body
    assert "no stated end" in body  # QBI Stage C has no end date
    assert "fact-verified_by_claude" in body and "fact-signed_off" in body
    assert 'hx-post="/commands/mark-catalyst"' in body
    api = client.get("/api/catalysts").json()
    assert {i["symbol"] for i in api["items"]} >= {"IBM", "QNT", "IONQ"}
    # Overview shows the next 12 months.
    assert "Catalysts and earnings, next 12 months" in client.get("/").text


def test_catalysts_page_requires_login(settings: object) -> None:
    from tests.conftest import make_client

    with make_client(settings, logged_in=False) as c:  # type: ignore[arg-type]
        assert c.get("/catalysts", follow_redirects=False).status_code == 303


def test_mark_command_csrf_validation_and_worker(rw_engine: Engine, client: TestClient) -> None:
    seed_repo_catalysts(rw_engine)
    with rw_engine.connect() as conn:
        cid = conn.execute(
            select(catalysts.c.id).where(catalysts.c.key == "seed:qbi_stage_c_qnt")
        ).scalar_one()
    assert client.post("/commands/mark-catalyst", data={"catalyst_id": cid}).status_code == 403
    client.get("/catalysts")
    h = {"X-CSRF-Token": client.cookies.get(COOKIE_NAME) or ""}
    bad = client.post(
        "/commands/mark-catalyst", data={"catalyst_id": cid, "status": "maybe"}, headers=h
    )
    assert bad.status_code == 400
    ok = client.post(
        "/commands/mark-catalyst",
        data={"catalyst_id": str(cid), "status": "cancelled", "note": "synthetic, with a comma"},
        headers=h,
    )
    assert ok.status_code == 202
    from aether.catalysts.mark import CatalystMark, apply_mark

    handlers = {
        "mark_catalyst": lambda a: apply_mark(
            rw_engine,
            CatalystMark.model_validate({k: v for k, v in a.items() if k != "_command_id"}),
        )
    }
    process_commands(rw_engine, handlers=handlers)
    with rw_engine.connect() as conn:
        status = conn.execute(select(commands.c.status)).scalars().all()
        row = conn.execute(select(catalysts).where(catalysts.c.id == cid)).one()
    assert status == ["done"]
    assert (row.status, row.resolution, row.note) == (
        "cancelled",
        "owner",
        "synthetic, with a comma",
    )
    assert "marked by you" in client.get("/catalysts").text


def test_ticker_page_panels(rw_engine: Engine, client: TestClient) -> None:
    seed_repo_catalysts(rw_engine)
    with write_tx(rw_engine) as conn:
        conn.execute(
            insert(short_interest).values(
                symbol="QNT",
                settlement_date="2026-09-15",
                short_shares=1_000_000,
                days_to_cover=2.5,
                pct_shares_out=4.2,
                shares_out=23_809_524,
                shares_out_as_of="2026-07-31",
                source="synthetic",
                source_url="https://example.test/shrt20260915.csv",
                fetched_at="2026-10-01T00:00:00Z",
            )
        )
        metrics = {
            "atm_iv_30": 0.8,
            "term": {"30": 0.8, "60": 0.75, "90": 0.7},
            "skew_30": -0.05,
            "iv_rank": None,
            "iv_history_days": 3,
            "put_call_volume": 0.6,
            "put_call_oi": 0.9,
            "volume_vs_median": None,
            "implied_moves": [
                {
                    "catalyst_id": 1,
                    "label": "QNT lock-up",
                    "date": "2026-11-30",
                    "expiry": "2026-12-18",
                    "straddle": 9.1,
                    "move": 0.152,
                    "strike": 60.0,
                }
            ],
        }
        upsert(
            conn,
            options_snapshots,
            [
                {
                    "symbol": "QNT",
                    "d": "2026-10-02",
                    "metrics": json.dumps(metrics),
                    "quality": json.dumps(
                        {"thin": False, "reasons": {"iv_rank": "building history (3 days of 252)"}}
                    ),
                    "provider": "synthetic",
                    "fetched_at": "2026-10-02T00:00:00Z",
                },
                {
                    "symbol": "IONQ",
                    "d": "2026-10-02",
                    "metrics": json.dumps({"atm_iv_30": None}),
                    "quality": json.dumps({"thin": True, "reasons": {}}),
                    "provider": "synthetic",
                    "fetched_at": "2026-10-02T00:00:00Z",
                },
            ],
            key_cols=["symbol", "d"],
        )
    body = client.get("/t/QNT").text
    no_inline(body)
    assert "QNT IPO lock-up ends" in body
    assert "4.2%" in body and "% of shares outstanding" in body
    assert "Options (research only)" in body and "±15.2%" in body
    assert "building history (3 days of 252)" in body
    thin = client.get("/t/IONQ").text
    assert "Thin chain: not reported." in thin and "±" not in thin
    assert "Options (research only)" not in client.get("/t/IBM").text  # context ticker


def test_review_pack_text_has_catalysts_and_options_but_no_dollars() -> None:
    pack = {
        "as_of": "2026-11-01",
        "profile": "safe",
        "targets": {
            "as_of": "2026-11-01",
            "prices_as_of": "2026-10-30",
            "weights": {"QTUM": 1.0},
            "chain": [],
            "note": None,
        },
        "plan": None,
        "flags": [],
        "upcoming": {"earnings": [], "lockups": []},
        "catalysts": [
            {
                "window_start": "2026-11-30",
                "window_end": "2026-11-30",
                "title": "QNT IPO lock-up ends",
                "fact_status": "verified_by_claude",
            },
            {
                "window_start": "2026-11-06",
                "window_end": None,
                "title": "QBI Stage C decision",
                "symbol": "IONQ",
                "fact_status": "signed_off",
            },
        ],
        "options": [
            {
                "symbol": "QNT",
                "d": "2026-10-30",
                "thin": False,
                "atm_iv_30": 0.8,
                "iv_rank": None,
                "iv_history_days": 20,
                "implied_moves": [{"move": 0.152, "date": "2026-11-30", "label": "lock-up"}],
            },
            {"symbol": "IONQ", "d": "2026-10-30", "thin": True},
            {"symbol": "RGTI", "d": None},
        ],
    }
    t = telegram_text(pack)
    assert "$" not in t and len(t) <= 4096
    assert "- 2026-11-30 QNT IPO lock-up ends [unconfirmed]" in t
    assert "- 2026-11-06 onwards IONQ: QBI Stage C decision" in t
    assert "QNT: IV30 80%, rank: building history (20 days); implied move ±15.2%" in t
    assert "IONQ: thin chain, not reported" in t and "RGTI: no snapshot yet" in t
    assert (
        options_line({"symbol": "X", "d": "d", "thin": False, "atm_iv_30": None, "iv_rank": 0.5})
        == "- X: IV30 n/a, rank 50%"
    )


# --------------------------------------------------------------------------- migration 0009


def test_0009_rebuilds_events_keeping_children_and_allows_finra(tmp_path: Path) -> None:
    path = tmp_path / "m8.db"
    ensure_db_file(path)
    migrate.upgrade(path, "0008_classify")
    eng = make_rw_engine(path)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO tickers (symbol, type) VALUES ('ACME', 'pure_play')"))
        conn.execute(
            text(
                "INSERT INTO events (url_hash, title, url, source_domain, trust_tier, published_at,"
                " origin, created_at) VALUES (x'01', 'ACME 8-K', 'https://example.test/a',"
                " 'example.test', 'T1', '2026-01-01T00:00:00Z', 'edgar', '2026-01-01T00:00:00Z')"
            )
        )
        for sql in (
            "INSERT INTO event_sources (event_id, url, domain, trust_tier) "
            "VALUES (1, 'https://example.test/a', 'example.test', 'T1')",
            "INSERT INTO event_tickers (event_id, symbol, direction) VALUES (1, 'ACME', -1)",
            "INSERT INTO event_classifications (event_id, class, category, materiality_raw,"
            " materiality, direction, confidence, rule_id, created_at) VALUES (1, 'RISK',"
            " 'dilution', 3, 3, -1, 1.0, 'r', '2026-01-01T00:00:00Z')",
            "INSERT INTO classify_state (event_id, status, updated_at) VALUES (1, 'done', 'x')",
        ):
            conn.execute(text(sql))
    eng.dispose()
    migrate.upgrade(path)
    eng = make_rw_engine(path)
    with eng.begin() as conn:
        for t in ("event_sources", "event_tickers", "event_classifications", "classify_state"):
            assert conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 1, t
        flags = dict(
            conn.execute(
                text(
                    "SELECT name, strict || wr FROM pragma_table_list "
                    "WHERE name IN ('events','event_sources','catalysts','short_interest')"
                )
            ).all()
        )
        assert flags == {
            "events": "10",
            "event_sources": "11",
            "catalysts": "10",
            "short_interest": "11",
        }
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        conn.execute(text("UPDATE events SET origin = 'finra'"))
        conn.execute(text("UPDATE event_sources SET origin = 'finra'"))
    with pytest.raises(IntegrityError), eng.begin() as conn:
        conn.execute(text("UPDATE events SET origin = 'gossip'"))
    eng.dispose()
