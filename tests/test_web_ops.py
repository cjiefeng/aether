"""M11 Ops page (spec §8.13): read-only; jobs, failing jobs, spend, escalations, evals, backups."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, insert
from starlette.testclient import TestClient

from aether.config import Settings
from aether.db.engine import write_tx
from aether.db.models import escalations, eval_runs, job_runs, llm_calls
from aether.db.types import to_iso
from aether.ops.backup import backup
from tests.conftest import seed_tickers
from tests.holdings_data import add_event


def _no_inline(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "style=" not in html


def test_ops_page_empty(client: TestClient) -> None:
    r = client.get("/ops")
    assert r.status_code == 200
    for text in (
        "No job runs recorded yet",
        "No escalations yet",
        "No backups yet",
        "No restore drill yet",
        "No eval results yet",
        "Used today (SGT): <strong>0 / 5",
    ):
        assert text in r.text, text
    assert 'href="/ops" aria-current="page"' in r.text
    _no_inline(r.text)


def test_ops_page_with_data(
    rw_engine: Engine, client: TestClient, settings: Settings, migrated_db: Path
) -> None:
    seed_tickers(rw_engine, [("ACME", "pure_play"), ("DEMO", "pure_play")])
    now = datetime.now(UTC)
    old = to_iso(now - timedelta(hours=30))
    e1 = add_event(rw_engine, "ACME", to_iso(now - timedelta(hours=1)), 5)
    e2 = add_event(rw_engine, "ACME", to_iso(now - timedelta(minutes=30)), 5)
    with write_tx(rw_engine) as conn:
        conn.execute(
            insert(job_runs),
            [
                {
                    "job": "edgar",
                    "started_at": old,
                    "finished_at": old,
                    "status": "failed",
                    "provider": None,
                    "error": "synthetic outage",
                },
                {
                    "job": "prices",
                    "started_at": to_iso(now),
                    "finished_at": to_iso(now),
                    "status": "ok",
                    "provider": "yfinance",
                    "error": None,
                },
                {
                    "job": "restore_drill",
                    "started_at": to_iso(now),
                    "finished_at": to_iso(now),
                    "status": "ok",
                    "provider": "aether-20261008.db",
                    "error": None,
                },
            ],
        )
        conn.execute(
            insert(llm_calls).values(
                purpose="research_verify",
                model="claude-opus-5-5",
                cost_micros=Decimal("1.25"),
                created_at=to_iso(now),
            )
        )
        conn.execute(
            insert(escalations),
            [
                {
                    "event_id": e1,
                    "symbol": "ACME",
                    "trigger": "materiality",
                    "status": "done",
                    "refusal": None,
                    "detail": json.dumps({"verify": {"status": "done"}}),
                    "created_at": to_iso(now),
                },
                {
                    "event_id": e2,
                    "symbol": "ACME",
                    "trigger": "materiality",
                    "status": "refused",
                    "refusal": "ticker_cooldown",
                    "detail": "{}",
                    "created_at": to_iso(now),
                },
            ],
        )
        conn.execute(
            insert(eval_runs).values(
                prompt_version="classify-v1-test",
                model="claude-sonnet-5-5",
                created_at=to_iso(now),
                n_items=60,
                provisional=1,
                passed=1,
                metrics=json.dumps(
                    {
                        "class_agreement": 0.9,
                        "risk_recall": 1.0,
                        "adversarial": {"flagged": 5, "n": 5},
                    }
                ),
            )
        )
    backup(migrated_db, settings.resolved_backup_dir)

    r = client.get("/ops")
    assert r.status_code == 200
    t = r.text
    assert "1 job failing for more than 24h" in t and "synthetic outage" in t
    assert "research_verify" in t and "$1.25" in t
    assert "Used today (SGT): <strong>1 / 5</strong>, 1 refused" in t
    assert "refused: ticker cooldown" in t and f'href="/feed?event={e1}"' in t
    assert "classify-v1-test" in t and "90%" in t and "5/5" in t
    assert f"aether-{now:%Y%m%d}.db" in t and "Last restore drill" in t
    _no_inline(t)


def test_health_links_to_ops(client: TestClient) -> None:
    assert 'href="/ops"' in client.get("/health").text
