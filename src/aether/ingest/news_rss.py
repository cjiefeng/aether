"""Job `news_rss` (M6, hourly): read every feed in `sources.yaml`, keep items about the watchlist or
the theme, and write them as unclassified events (`ingest/news_events.py`).

Network first (all feeds), then one short write transaction for the whole run. Classification is
M7; nothing here calls an LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select

from aether.config import SLEEVE_TYPES, Feed, Sources, Watchlist
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import feed_state, tickers
from aether.db.types import to_iso
from aether.edgar.text import html_to_text
from aether.ingest.news_events import Matcher, NewsItem, canonical_url, write_news_item
from aether.providers.prices import ProviderError
from aether.providers.rss import FeedEntry, FeedResponse, RssClient, parse_feed
from aether.runs import JobResult

log = logging.getLogger(__name__)


@dataclass
class FeedOutcome:
    feed: Feed
    response: FeedResponse | None
    entries: list[FeedEntry]
    error: str | None


def build_matcher(engine: Engine, watchlist: Watchlist, sources: Sources) -> Matcher:
    with engine.connect() as conn:
        known = set(conn.execute(select(tickers.c.symbol)).scalars())
    rows = [
        (t.symbol, t.aliases)
        for t in watchlist.tickers
        if t.active and t.type in ("etf", *SLEEVE_TYPES) and t.symbol in known
    ]
    return Matcher.build(rows, sources.theme_keywords)


def to_item(
    feed: Feed, entry: FeedEntry, matcher: Matcher, known: set[str], now: datetime
) -> NewsItem | None:
    """The stored item, or None when it's off-topic or unusable."""
    if not entry.title or not entry.link:
        return None
    try:
        canonical_url(entry.link)
    except ValueError:
        return None
    summary = html_to_text(entry.summary) if entry.summary else ""
    text = f"{entry.title}\n{summary}"
    symbols = set(matcher.symbols(text))
    if feed.symbol and feed.symbol in known:
        symbols.add(feed.symbol)
    if not symbols and not feed.symbol and not matcher.is_theme(text):
        return None
    raw: dict[str, Any] = {"feed": feed.id}
    published = entry.published
    if published is None:
        published, raw["date_source"] = now, "retrieved"
    return NewsItem(
        url=entry.link,
        title=entry.title,
        published_at=published,
        origin="rss",
        excerpt=summary or None,
        symbols=tuple(sorted(symbols)),
        raw=raw,
    )


def ingest_news_rss(
    engine: Engine,
    client: RssClient,
    sources: Sources,
    watchlist: Watchlist,
    now: datetime | None = None,
) -> JobResult:
    now = now or datetime.now(UTC)
    if not sources.feeds:
        return JobResult(provider="rss", warning="no feeds configured")
    with engine.connect() as conn:
        state = {r.feed_id: r for r in conn.execute(select(feed_state)).all()}
        known = set(conn.execute(select(tickers.c.symbol)).scalars())
    matcher = build_matcher(engine, watchlist, sources)

    outcomes: list[FeedOutcome] = []
    for feed in sources.feeds:
        st = state.get(feed.id)
        same_url = st is not None and st.url == feed.url
        try:
            resp = client.fetch(
                feed.url,
                etag=st.etag if same_url and st else None,
                last_modified=st.last_modified if same_url and st else None,
            )
            entries = parse_feed(resp.text, feed.url) if resp.status == 200 else []
            outcomes.append(FeedOutcome(feed, resp, entries, None))
        except ProviderError as exc:
            log.warning("feed %s failed: %s", feed.id, exc)
            outcomes.append(FeedOutcome(feed, None, [], str(exc)[:300]))

    created = merged = 0
    now_iso = to_iso(now)
    with write_tx(engine) as conn:
        for o in outcomes:
            kept = 0
            for entry in o.entries:
                item = to_item(o.feed, entry, matcher, known, now)
                if item is None:
                    continue
                kept += 1
                res = write_news_item(conn, item, sources, now)
                created += int(res.created)
                merged += int(res.merged)
            prev = state.get(o.feed.id)
            row: dict[str, Any] = {
                "feed_id": o.feed.id,
                "url": o.feed.url,
                "last_fetched_at": now_iso,
                "last_status": o.response.status if o.response else None,
                "last_error": o.error
                or ("; ".join(o.response.notes) if o.response and o.response.notes else None),
                "items_seen": len(o.entries),
                "items_kept": kept,
            }
            if o.response is not None and o.response.status == 200:
                row["etag"] = o.response.etag
                row["last_modified"] = o.response.last_modified
            elif o.response is not None and o.response.status == 304 and prev is not None:
                row["items_seen"], row["items_kept"] = 0, 0
            upsert(conn, feed_state, [row], key_cols=["feed_id"])

    failed = [o.feed.id for o in outcomes if o.error]
    if failed and len(failed) == len(outcomes):
        raise RuntimeError(f"all feeds failed: {', '.join(failed)}")
    return JobResult(
        rows_written=created + merged,
        provider="rss",
        warning=f"feeds failed: {', '.join(failed)}" if failed else None,
    )
