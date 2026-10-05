"""Feed page (M7, spec §8 item 3): filters, NOISE hidden by default, quarantine warning, caps
shown, login required, untrusted text escaped (S5). Synthetic items only."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import Engine
from starlette.testclient import TestClient

from aether.classify.pipeline import write_classification
from aether.config import SourceDomain, Sources
from aether.db.engine import write_tx
from aether.ingest.news_events import NewsItem, write_news_item
from tests.conftest import seed_tickers

NOW = datetime.now(UTC)
SOURCES = Sources(
    domains=(
        SourceDomain(domain="acme-ir.test", tier="T1"),
        SourceDomain(domain="trade-press.test", tier="T2"),
    )
)


def _add(conn, url: str, title: str, sym: str, cls: str, cat: str, raw: int, **kw: object) -> int:  # type: ignore[no-untyped-def]
    eid = write_news_item(
        conn, NewsItem(url, title, NOW, "rss", excerpt="Synthetic excerpt.", symbols=(sym,)),
        SOURCES, NOW,
    ).event_id  # fmt: skip
    write_classification(
        conn,
        eid,
        cls=cls,
        category=cat,
        materiality_raw=raw,
        direction=-1 if cls == "RISK" else 0,
        directions={sym: -1 if cls == "RISK" else 0},
        confidence=0.7,
        rationale=str(kw.get("rationale", "Synthetic rationale.")),
        evidence_quote="Synthetic excerpt",
        rule_id=None,
        model="claude-sonnet-5-5",
        version="classify-v1-test",
        quarantine=bool(kw.get("quarantine", False)),
        now=NOW,
    )
    return eid


def _seed(engine: Engine) -> None:
    seed_tickers(engine, [("QTUM", "etf"), ("IONQ", "pure_play"), ("RGTI", "pure_play")])
    with write_tx(engine) as conn:
        _add(
            conn, "https://acme-ir.test/a", "IONQ synthetic offering", "IONQ", "RISK", "dilution", 4
        )
        _add(
            conn,
            "https://random.test/b",
            "RGTI synthetic <script>alert(1)</script> contract",
            "RGTI",
            "SIGNAL",
            "contract_with_value",
            5,
            rationale="Model <b>rationale</b>",
        )
        _add(
            conn,
            "https://trade-press.test/c",
            "IONQ synthetic stock list",
            "IONQ",
            "NOISE",
            "listicle_or_momentum",
            1,
        )
        _add(conn, "https://trade-press.test/d", "RGTI synthetic injected item", "RGTI", "RISK",
             "going_concern", 3, quarantine=True)  # fmt: skip


def _no_inline(html: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "style=" not in html


def test_feed_renders_escaped_with_caps_and_quarantine(
    rw_engine: Engine, client: TestClient
) -> None:
    _seed(rw_engine)
    r = client.get("/feed")
    assert r.status_code == 200
    _no_inline(r.text)
    assert "&lt;script&gt;alert(1)" in r.text and "Model &lt;b&gt;rationale" in r.text
    assert "IONQ synthetic offering" in r.text
    # T3-only SIGNAL raw 5 → shown capped at 2.
    assert "materiality 2/5" in r.text and "capped from 5" in r.text
    # NOISE hidden by default; quarantined shown with a warning.
    assert "IONQ synthetic stock list" not in r.text
    assert "RGTI synthetic injected item" in r.text and "Quarantined:" in r.text
    assert "classify-v1-test" in r.text
    assert "No eval result for this prompt version yet" in r.text
    assert 'href="/feed"' in r.text  # nav link


def test_feed_filters(rw_engine: Engine, client: TestClient) -> None:
    _seed(rw_engine)
    r = client.get("/feed?noise=1")
    assert "IONQ synthetic stock list" in r.text
    r = client.get("/feed?cls=NOISE")
    assert "IONQ synthetic stock list" in r.text and "IONQ synthetic offering" not in r.text
    r = client.get("/feed?symbol=RGTI")
    assert "IONQ synthetic offering" not in r.text and "RGTI synthetic injected item" in r.text
    r = client.get("/feed?min_materiality=4")
    assert "IONQ synthetic offering" in r.text and "RGTI synthetic injected item" not in r.text
    r = client.get("/feed?tier=T1")
    assert "IONQ synthetic offering" in r.text and "RGTI synthetic" not in r.text
    r = client.get("/feed?category=dilution")
    assert "IONQ synthetic offering" in r.text and "injected" not in r.text
    r = client.get("/feed?cls=EVIL&category=nope&symbol=X&min_materiality=abc&tier=T9")
    assert r.status_code == 200 and "IONQ synthetic offering" in r.text


def test_news_page_shows_class_badges(rw_engine: Engine, client: TestClient) -> None:
    _seed(rw_engine)
    r = client.get("/news")
    assert 'class="badge risk"' in r.text and 'class="badge signal"' in r.text


def test_feed_requires_login(settings) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import make_client

    with make_client(settings, logged_in=False) as c:
        r = c.get("/feed", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
