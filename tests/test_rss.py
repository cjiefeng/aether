"""RSS ingest (M6) on feeds recorded 2026-10-05 (`make record-cassette`), replayed through respx."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine, func, select

from aether.config import Feed, Sources, load_sources, load_watchlist
from aether.db.models import event_sources, event_tickers, events, feed_state
from aether.ingest.news_rss import ingest_news_rss
from aether.providers.prices import ProviderError
from aether.providers.rss import RssClient, parse_date, parse_feed
from aether.worker import sync_config
from tests.cassettes import load_cassette
from tests.conftest import CONFIG_DIR, make_settings

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CASSETTES = {
    "rigetti_ir": "rss_rigetti_ir",
    "quantinuum_ir": "rss_quantinuum_ir",
    "infleqtion_news": "rss_infleqtion",
    "thequantuminsider": "rss_thequantuminsider",
    "quantumcomputingreport": "rss_quantumcomputingreport",
}


@pytest.fixture
def engine(rw_engine: Engine, migrated_db: Path) -> Engine:
    sync_config(rw_engine, make_settings(migrated_db))  # real watchlist → tickers
    return rw_engine


def _mount(router: respx.MockRouter, sources: Sources, robots_status: int = 404) -> None:
    for feed in sources.feeds:
        c = load_cassette(CASSETTES[feed.id])
        resp = c["response"]
        router.get(feed.url).mock(
            return_value=httpx.Response(resp["status"], headers=resp["headers"], text=resp["text"])
        )
    router.get(url__regex=r"https://[^/]+/robots\.txt").mock(
        return_value=httpx.Response(robots_status)
    )


def _run(engine: Engine, sources: Sources | None = None):  # type: ignore[no-untyped-def]
    sources = sources or load_sources(CONFIG_DIR)
    client = RssClient(httpx.Client())
    return ingest_news_rss(engine, client, sources, load_watchlist(CONFIG_DIR), now=NOW)


def _count(engine: Engine, table) -> int:  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


@respx.mock
def test_recorded_feeds_ingest(engine: Engine) -> None:
    sources = load_sources(CONFIG_DIR)
    _mount(respx.mock, sources)
    result = _run(engine)
    assert result is not None and result.warning is None and result.provider == "rss"
    with engine.connect() as conn:
        state = {r.feed_id: r for r in conn.execute(select(feed_state)).all()}
        rows = conn.execute(select(events)).all()
        tick = conn.execute(select(event_tickers.c.symbol, func.count()).group_by("symbol")).all()
    assert set(state) == set(CASSETTES)
    assert all(s.last_status == 200 and s.items_seen == 10 for s in state.values())
    # IR feeds are pinned to their ticker: every item kept.
    for fid in ("rigetti_ir", "quantinuum_ir", "infleqtion_news"):
        assert state[fid].items_kept == 10
    # Industry press keeps only watchlist/theme items.
    for fid in ("thequantuminsider", "quantumcomputingreport"):
        assert state[fid].items_kept < 10
    counts = dict(tick)
    assert counts["RGTI"] >= 10 and counts["QNT"] >= 10 and counts["INFQ"] >= 10
    assert rows and all(r.origin == "rss" for r in rows)
    # Spec M6 acceptance: excerpts ≤ 600 chars.
    assert all(r.excerpt is None or len(r.excerpt) <= 600 for r in rows)
    tiers = {r.source_domain: r.trust_tier for r in rows}
    assert tiers.get("investors.rigetti.com") == "T1"
    assert tiers.get("ir.quantinuum.com") == "T1"
    assert all(t == "T2" for d, t in tiers.items() if d.endswith("thequantuminsider.com"))
    assert result.rows_written == len(rows) + _merged(engine)


def _merged(engine: Engine) -> int:
    return _count(engine, event_sources) - _count(engine, events)


@respx.mock
def test_rerun_is_idempotent_and_honours_304(engine: Engine) -> None:
    sources = load_sources(CONFIG_DIR)
    _mount(respx.mock, sources)
    _run(engine)
    n_events, n_sources = _count(engine, events), _count(engine, event_sources)
    assert _run(engine).rows_written == 0  # same items again
    assert (_count(engine, events), _count(engine, event_sources)) == (n_events, n_sources)

    respx.mock.reset()
    seen_headers: list[httpx.Headers] = []

    def not_modified(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(304)

    for feed in sources.feeds:
        respx.get(feed.url).mock(side_effect=not_modified)
    respx.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))
    assert _run(engine).rows_written == 0
    with engine.connect() as conn:
        assert set(conn.execute(select(feed_state.c.last_status)).scalars()) == {304}
    # Conditional GET used the stored validators where the server sent any.
    assert any("if-none-match" in h or "if-modified-since" in h for h in seen_headers)


@respx.mock
def test_robots_disallow_skips_that_feed(engine: Engine) -> None:
    sources = Sources(
        domains=(),
        feeds=(
            Feed(id="blocked", url="https://blocked.example.test/feed"),
            Feed(id="open", url="https://open.example.test/feed", symbol="IONQ"),
        ),
    )
    respx.get("https://blocked.example.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /feed\n")
    )
    blocked = respx.get("https://blocked.example.test/feed")
    respx.get("https://open.example.test/robots.txt").mock(side_effect=httpx.ConnectTimeout("t"))
    respx.get("https://open.example.test/feed").mock(
        return_value=httpx.Response(
            200, text=_rss([("ACME synthetic item", "https://open.example.test/1")])
        )
    )
    result = _run(engine, sources)
    assert not blocked.called
    assert result.warning == "feeds failed: blocked"
    with engine.connect() as conn:
        st = {r.feed_id: r for r in conn.execute(select(feed_state)).all()}
    assert st["blocked"].last_error == "rss: disallowed by robots.txt"
    assert st["open"].last_status == 200 and "robots.txt unreachable" in st["open"].last_error


@respx.mock
def test_all_feeds_failing_fails_the_job(engine: Engine) -> None:
    sources = Sources(domains=(), feeds=(Feed(id="down", url="https://down.example.test/feed"),))
    respx.get("https://down.example.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://down.example.test/feed").mock(return_value=httpx.Response(503))
    with pytest.raises(RuntimeError, match="all feeds failed"):
        _run(engine, sources)


@respx.mock
def test_topic_filter_and_ticker_matching(engine: Engine) -> None:
    sources = Sources(
        domains=(),
        feeds=(Feed(id="press", url="https://press.example.test/feed"),),
        theme_keywords=("logical qubit",),
    )
    items = [
        ("IonQ signs a synthetic test agreement", "https://press.example.test/1"),
        ("Synthetic note on QBTS and a cooking recipe", "https://press.example.test/2"),
        ("A qnt lowercase mention is not a ticker", "https://press.example.test/3"),
        ("Lab reports a synthetic Logical Qubit record", "https://press.example.test/4"),
        ("Unrelated synthetic gardening news", "https://press.example.test/5"),
        ("Bad link item", "javascript:alert(1)"),
    ]
    respx.get("https://press.example.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://press.example.test/feed").mock(
        return_value=httpx.Response(200, text=_rss(items))
    )
    _run(engine, sources)
    with engine.connect() as conn:
        rows = conn.execute(select(events.c.id, events.c.url)).all()
        tick = conn.execute(select(event_tickers.c.event_id, event_tickers.c.symbol)).all()
    urls = {u for _i, u in rows}
    assert urls == {
        "https://press.example.test/1",
        "https://press.example.test/2",
        "https://press.example.test/4",
    }
    by_url = {u: i for i, u in rows}
    assert {
        (by_url[u], s)
        for u, s in [
            ("https://press.example.test/1", "IONQ"),
            ("https://press.example.test/2", "QBTS"),
        ]
    } <= set(tick)
    assert not any(e == by_url["https://press.example.test/4"] for e, _s in tick)  # theme only


def _rss(items: list[tuple[str, str]], extra: str = "") -> str:
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link>"
        "<pubDate>Mon, 05 Oct 2026 08:00:00 +0000</pubDate>"
        f"<description>&lt;p&gt;{t} body&lt;/p&gt;</description></item>"
        for t, u in items
    )
    head = f'<?xml version="1.0"?>{extra}<rss version="2.0"><channel><title>t</title>'
    return f"{head}{body}</channel></rss>"


def test_dtd_rejected() -> None:
    xxe = _rss(
        [("x", "https://e.test/1")], extra='<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
    )
    with pytest.raises(ProviderError, match="DTD"):
        parse_feed(xxe, "https://e.test/feed")


def test_atom_and_dates() -> None:
    atom = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
        '<entry><title>ACME atom item</title><link rel="alternate" href="/a/1"/>'
        "<updated>2026-10-04T10:00:00Z</updated><summary>s</summary></entry></feed>"
    )
    (e,) = parse_feed(atom, "https://e.test/feed")
    assert e.link == "https://e.test/a/1"
    assert e.published == datetime(2026, 10, 4, 10, tzinfo=UTC)
    assert parse_date("Tue, 08 Sep 2026 07:00:19 -0400") == datetime(
        2026, 9, 8, 11, 0, 19, tzinfo=UTC
    )
    assert parse_date("2026-10-04") is None  # no zone: unknown
    assert parse_date("garbage") is None


def test_only_https_feeds() -> None:
    with pytest.raises(ProviderError, match="https"):
        RssClient(httpx.Client()).fetch("http://e.test/feed")
