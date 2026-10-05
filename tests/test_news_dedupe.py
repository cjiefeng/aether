"""News dedupe and independent-source counting (M6, spec §5.2 step 4). Synthetic items only:
example.test-style domains and ACME headlines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select

from aether.config import SourceDomain, Sources
from aether.db.engine import write_tx
from aether.db.models import event_sources, event_tickers, events
from aether.ingest.news_events import (
    EXCERPT_MAX,
    NewsItem,
    canonical_url,
    clip_excerpt,
    hamming,
    normalize_title,
    registrable_domain,
    simhash64,
    write_news_item,
)
from tests.conftest import seed_tickers

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SOURCES = Sources(
    domains=(
        SourceDomain(domain="acme-ir.test", tier="T1"),
        SourceDomain(domain="trade-press.test", tier="T2"),
        SourceDomain(domain="other-press.test", tier="T2"),
    ),
    syndicators=("wire-one.test",),
)
TITLE = "ACME Announces Synthetic Widget Contract With Example Agency"
BODY = (
    "ACME Corp (NYSE: ACME) today announced a synthetic contract with the Example Agency for "
    "widgets. This press release is a test fixture and describes no real company or event."
)


@pytest.fixture
def engine(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, [("ACME", "pure_play"), ("BETA", "pure_play")])
    return rw_engine


def _item(url: str, title: str = TITLE, body: str | None = BODY, **kw: object) -> NewsItem:
    return NewsItem(
        url=url,
        title=title,
        published_at=kw.pop("published_at", NOW),  # type: ignore[arg-type]
        origin=kw.pop("origin", "rss"),  # type: ignore[arg-type]
        excerpt=body,
        symbols=kw.pop("symbols", ("ACME",)),  # type: ignore[arg-type]
    )


def _write(engine: Engine, *items: NewsItem):  # type: ignore[no-untyped-def]
    with write_tx(engine) as conn:
        return [write_news_item(conn, it, SOURCES, NOW) for it in items]


def _event(engine: Engine, event_id: int):  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        return conn.execute(select(events).where(events.c.id == event_id)).one()


def _count(engine: Engine, table) -> int:  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def test_three_syndicated_copies_are_one_event_with_independent_count_one(engine: Engine) -> None:
    """Spec M6 acceptance: 3 syndicated copies → 1 event with independent count 1."""
    results = _write(
        engine,
        _item("https://news-a.test/2026/acme-widget-contract"),
        _item("https://news-b.test/markets/acme-widget?utm_source=rss"),  # same body: a copy
        _item("https://www.wire-one.test/release/123 ", title=TITLE + " - Wire One"),  # a wire
    )
    assert [r.created for r in results] == [True, False, False]
    assert [r.merged for r in results] == [False, True, True]
    assert all(r.event_id == results[0].event_id for r in results)
    assert _count(engine, events) == 1
    assert _count(engine, event_sources) == 3
    ev = _event(engine, results[0].event_id)
    assert ev.independent_source_count == 1
    with engine.connect() as conn:
        synd = conn.execute(
            select(event_sources.c.domain, event_sources.c.syndicated).order_by(
                event_sources.c.domain
            )
        ).all()
    assert synd == [("news-a.test", 0), ("news-b.test", 1), ("wire-one.test", 1)]


def test_independent_outlets_count_separately(engine: Engine) -> None:
    other_body = (
        "Trade Press reports that ACME won a synthetic widget award from the Example Agency, "
        "citing its own sources; this is independent reporting in a test fixture."
    )
    third_body = (
        "Other Press covered the ACME widget contract in its weekly synthetic roundup and added "
        "comment from a fictional analyst, all of it invented for this test."
    )
    r = _write(
        engine,
        _item("https://acme-ir.test/news/widget"),
        _item("https://trade-press.test/a/1", body=other_body),
        _item("https://other-press.test/b/2", body=third_body),
        _item("https://sub.trade-press.test/a/1-amp", body=other_body),  # same domain + same body
    )
    ev = _event(engine, r[0].event_id)
    assert ev.independent_source_count == 3  # acme-ir, trade-press, other-press
    assert ev.trust_tier == "T1"  # best tier of the sources


def test_same_url_is_idempotent_and_adds_tickers(engine: Engine) -> None:
    a = _write(engine, _item("https://news-a.test/x?utm_medium=feed#top"))[0]
    b = _write(engine, _item("http://NEWS-A.test/x/", symbols=("BETA",)))[0]
    assert b.event_id == a.event_id and not b.created and not b.merged
    assert _count(engine, events) == 1 and _count(engine, event_sources) == 1
    with engine.connect() as conn:
        syms = set(conn.execute(select(event_tickers.c.symbol)).scalars())
    assert syms == {"ACME", "BETA"}


def test_different_story_is_a_new_event(engine: Engine) -> None:
    r = _write(
        engine,
        _item("https://news-a.test/1"),
        _item("https://news-a.test/2", title="BETA Reports Synthetic Quarterly Results", body=None),
    )
    assert r[1].created and _count(engine, events) == 2


def test_merge_window_is_seven_days(engine: Engine) -> None:
    r = _write(
        engine,
        _item("https://news-a.test/1"),
        _item("https://news-b.test/1", published_at=NOW + timedelta(days=8)),
    )
    assert r[1].created


def test_earliest_report_dates_the_story(engine: Engine) -> None:
    r = _write(
        engine,
        _item("https://news-a.test/1"),
        _item("https://news-b.test/1", published_at=NOW - timedelta(hours=5)),
    )
    assert _event(engine, r[0].event_id).published_at == "2026-10-05T07:00:00Z"


def test_excerpt_capped(engine: Engine) -> None:
    """Spec M6 acceptance: excerpts ≤ 600 chars (≤ 500 enforced in code)."""
    long_body = "ACME synthetic word " * 400  # 8,000 chars
    (r,) = _write(engine, _item("https://news-a.test/long", body=long_body))
    ev = _event(engine, r.event_id)
    assert ev.excerpt is not None and len(ev.excerpt) <= EXCERPT_MAX <= 600
    with engine.connect() as conn:
        src = conn.execute(select(event_sources.c.excerpt)).scalar_one()
    assert len(src) <= EXCERPT_MAX


def test_edgar_events_never_merged(engine: Engine) -> None:
    from aether.ingest.news_events import url_hash

    with write_tx(engine) as conn:
        conn.execute(
            events.insert().values(
                url_hash=url_hash("https://www.sec.gov/acme-8k"),
                simhash=None,
                title=TITLE,
                url="https://www.sec.gov/acme-8k",
                source_domain="sec.gov",
                trust_tier="T1",
                published_at="2026-10-05T12:00:00Z",
                origin="edgar",
                created_at="2026-10-05T12:00:00Z",
            )
        )
    (r,) = _write(engine, _item("https://news-a.test/1"))
    assert r.created


# --------------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("raw", "canon"),
    [
        (
            "HTTP://WWW.Example.TEST/a/b/?utm_source=x&b=2&a=1#frag",
            "https://example.test/a/b?a=1&b=2",
        ),
        ("https://example.test", "https://example.test/"),
        ("https://example.test:8443/p?fbclid=1&gclid=2&ref=3", "https://example.test:8443/p"),
        ("https://example.test/p?mc_cid=1&id=7", "https://example.test/p?id=7"),
    ],
)
def test_canonical_url(raw: str, canon: str) -> None:
    assert canonical_url(raw) == canon


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://example.test/x", "/relative", ""])
def test_canonical_url_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        canonical_url(bad)


def test_registrable_domain() -> None:
    assert registrable_domain("a.b.example.test") == "example.test"
    assert registrable_domain("news.example.co.uk") == "example.co.uk"
    assert registrable_domain("www.example.com.sg") == "example.com.sg"


def test_simhash_near_duplicates() -> None:
    a = simhash64(normalize_title(TITLE))
    b = simhash64(normalize_title(TITLE + " | Wire One"))
    c = simhash64(normalize_title("BETA Reports Synthetic Quarterly Results"))
    assert a == simhash64(normalize_title(TITLE.upper()))  # case-insensitive, stable
    assert hamming(a, b) <= 3
    assert hamming(a, c) > 10
    assert simhash64("") == 0


def test_clip_excerpt() -> None:
    assert clip_excerpt("  a \n b  ") == "a b"
    assert clip_excerpt("") is None
    out = clip_excerpt("word " * 300)
    assert out is not None and len(out) <= EXCERPT_MAX and out.endswith("…")
