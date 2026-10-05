"""News page and the research-sweep command (M6): read-only rendering of untrusted items (S5)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Engine, insert, select
from starlette.testclient import TestClient

from aether.config import SourceDomain, Sources
from aether.db.engine import write_tx
from aether.db.models import commands, job_runs, llm_calls
from aether.db.types import to_iso
from aether.ingest.news_events import NewsItem, write_news_item
from aether.llm.pricing import sgt_day_start
from aether.security.csrf import COOKIE_NAME
from tests.conftest import seed_tickers

NOW = datetime.now(UTC)
SOURCES = Sources(domains=(SourceDomain(domain="trade-press.test", tier="T2"),))


def _no_inline(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "style=" not in html


def _seed(engine: Engine) -> None:
    seed_tickers(engine, [("QTUM", "etf"), ("IONQ", "pure_play"), ("QQQ", "benchmark")])
    with write_tx(engine) as conn:
        for url, title, sym in [
            ("https://trade-press.test/1", "IonQ <script>alert(1)</script> synthetic", "IONQ"),
            ("https://other.test/2", "QTUM synthetic fund item", "QTUM"),
        ]:
            write_news_item(
                conn,
                NewsItem(
                    url, title, NOW, "rss", excerpt="Synthetic <b>excerpt</b>", symbols=(sym,)
                ),
                SOURCES,
                NOW,
            )
        conn.execute(
            insert(llm_calls).values(
                purpose="research_sweep",
                model="claude-opus-5-5",
                cost_micros=Decimal("4.15"),
                created_at=to_iso(max(NOW, sgt_day_start(NOW))),
            )
        )
        conn.execute(
            insert(job_runs).values(
                job="news_rss", started_at=to_iso(NOW), finished_at=to_iso(NOW), status="ok"
            )
        )


def test_news_page_renders_escaped(rw_engine: Engine, client: TestClient) -> None:
    _seed(rw_engine)
    r = client.get("/news")
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)" in r.text
    assert "&lt;b&gt;excerpt" in r.text
    assert 'href="https://trade-press.test/1" rel="noopener noreferrer nofollow"' in r.text
    assert "T2" in r.text and "T3" in r.text
    assert "$4.15" in r.text and "83%" in r.text and "over 80%" in r.text
    assert "pending" in r.text  # not classified yet
    _no_inline(r.text)


def test_news_filters(rw_engine: Engine, client: TestClient) -> None:
    _seed(rw_engine)
    r = client.get("/news?symbol=QTUM")
    assert "QTUM synthetic fund item" in r.text and "IonQ &lt;script" not in r.text
    r = client.get("/news?origin=web_search")
    assert "No news items yet" in r.text
    r = client.get("/news?symbol=NOPE&origin=evil")  # unknown values ignored
    assert r.status_code == 200 and "QTUM synthetic fund item" in r.text


def test_ticker_page_news_card(rw_engine: Engine, client: TestClient) -> None:
    _seed(rw_engine)
    r = client.get("/t/QTUM")
    assert "Recent news" in r.text and "QTUM synthetic fund item" in r.text
    assert "Recent news" not in client.get("/t/QQQ").text


def test_stale_banner_when_rss_never_ran(client: TestClient) -> None:
    assert "The RSS job hasn&#39;t run yet." in client.get("/news").text


def test_research_sweep_command_needs_csrf(rw_engine: Engine, client: TestClient) -> None:
    assert client.post("/commands/research-sweep").status_code == 403
    token = client.cookies.get(COOKIE_NAME) or ""
    r = client.post("/commands/research-sweep", headers={"X-CSRF-Token": token})
    assert r.status_code == 202
    with rw_engine.connect() as conn:
        assert conn.execute(select(commands.c.kind)).scalars().all() == ["research_sweep"]


def test_news_requires_login(settings) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import make_client

    with make_client(settings, logged_in=False) as c:
        r = c.get("/news", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
