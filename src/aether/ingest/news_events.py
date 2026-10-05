"""Write RSS and research items as unclassified events, with dedupe and independent-source counting
(spec §5.2 step 4, M6).

Merge rule, in order:
1. the same canonical URL is the same event (tickers are added);
2. a title within `SIMHASH_MAX_DISTANCE` bits of a news/research event published within
   `MERGE_WINDOW` is the same story: the item becomes another `event_sources` row;
3. otherwise a new event.

A merged source is **syndicated** when its domain is a configured wire/mirror (`sources.yaml`
`syndicators`) or its excerpt is a near-copy of another source's excerpt (the same body text).
`independent_source_count` = distinct registrable domains among non-syndicated sources (min 1), so
a press release copied across three sites counts once. EDGAR events are never touched here.

All ingested text is untrusted (S1): it's stored as data and only ever rendered through S5 helpers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import Connection, insert, select, update

from aether.config import Sources, TrustTier
from aether.db.dialect import upsert
from aether.db.models import event_sources, event_tickers, events
from aether.db.types import i64_to_u64, to_iso, u64_to_i64

EXCERPT_MAX = 500  # S6: ~500 chars; the DB CHECK (600) is the backstop
TITLE_MAX = 300
SIMHASH_MAX_DISTANCE = 3
MERGE_WINDOW = timedelta(days=7)
# Excerpts shorter than this are too generic to prove two sources share a body.
MIN_EXCERPT_FOR_MATCH = 80
NEWS_ORIGINS = ("rss", "web_search")
_TIER_RANK: dict[str, int] = {"T1": 1, "T2": 2, "T3": 3}

_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "ref", "ref_src", "cmpid"})
_TRACKING_PREFIXES = ("utm_", "mc_", "_hs", "hsa_")
# Second-level suffixes where the registrable domain has three labels. Not the full Public Suffix
# List (no dependency); unknown multi-part suffixes fall back to the last two labels.
_MULTI_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "com.sg", "edu.sg",
        "gov.sg", "co.jp", "ne.jp", "com.cn", "com.hk", "co.in", "co.kr", "co.nz", "com.br",
        "com.tw", "co.za", "com.my",
    }
)  # fmt: skip
_WORD_RE = re.compile(r"[a-z0-9]+")
# A trailing " - Outlet" / " | Outlet" (≤4 words) is the publisher's name, not the headline.
_TITLE_SUFFIX_RE = re.compile(r"\s+[-|\u2013\u2014]\s+[^-|\u2013\u2014]{1,40}$")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class NewsItem:
    url: str
    title: str
    published_at: datetime  # timezone-aware
    origin: str  # 'rss' | 'web_search'
    excerpt: str | None = None
    symbols: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WriteResult:
    event_id: int
    created: bool
    merged: bool  # added as another source of an existing story
    syndicated: bool


# --------------------------------------------------------------------------- pure helpers


def canonical_url(url: str) -> str:
    """https, lowercase host without `www.`, no fragment, no tracking params, sorted query, no
    trailing slash. Raises ValueError for anything that isn't an absolute http(s) URL."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an absolute http(s) URL: {url[:100]!r}")
    host = parts.hostname.lower().rstrip(".")
    host = host.removeprefix("www.")
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
    )
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https", host + port, path, urlencode(query), ""))


def url_hash(canonical: str) -> bytes:
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host.removeprefix("www.")


def registrable_domain(host: str) -> str:
    labels = host.lower().rstrip(".").removeprefix("www.").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def normalize_title(title: str) -> str:
    t = _SPACE_RE.sub(" ", title).strip()
    stripped = _TITLE_SUFFIX_RE.sub("", t)
    # Only strip when a real headline remains (avoid eating "A - B" style titles).
    if len(stripped.split()) >= 4:
        t = stripped
    return t.lower()


def simhash64(text: str) -> int:
    """64-bit simhash over word unigrams + bigrams (unsigned). 0 for empty text."""
    words = _WORD_RE.findall(text.lower())
    if not words:
        return 0
    features = words + [f"{a} {b}" for a, b in pairwise(words)]
    acc = [0] * 64
    for f in features:
        h = int.from_bytes(hashlib.blake2b(f.encode("utf-8"), digest_size=8).digest(), "big")
        for i in range(64):
            acc[i] += 1 if h >> i & 1 else -1
    return sum(1 << i for i in range(64) if acc[i] > 0)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def clip_excerpt(text: str | None, limit: int = EXCERPT_MAX) -> str | None:
    """Whitespace-normalized excerpt of at most `limit` chars, cut on a word boundary."""
    if not text:
        return None
    t = _SPACE_RE.sub(" ", text).strip()
    if not t:
        return None
    if len(t) <= limit:
        return t
    cut = t[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def best_tier(tiers: Sequence[str]) -> TrustTier:
    best = min(tiers, key=lambda t: _TIER_RANK[t])
    assert best in ("T1", "T2", "T3")
    return best  # type: ignore[return-value]


# --------------------------------------------------------------------------- DB


def _excerpt_matches(excerpt_hash: int | None, others: Sequence[int | None]) -> bool:
    if excerpt_hash is None:
        return False
    return any(o is not None and hamming(excerpt_hash, i64_to_u64(o)) <= 3 for o in others)


def _recount(conn: Connection, event_id: int) -> None:
    rows = conn.execute(
        select(
            event_sources.c.domain, event_sources.c.trust_tier, event_sources.c.syndicated
        ).where(event_sources.c.event_id == event_id)
    ).all()
    independent = {registrable_domain(d) for d, _t, synd in rows if not synd}
    conn.execute(
        update(events)
        .where(events.c.id == event_id)
        .values(
            independent_source_count=max(1, len(independent)),
            trust_tier=best_tier([t for _d, t, _s in rows]),
        )
    )


def _add_tickers(conn: Connection, event_id: int, symbols: Sequence[str]) -> None:
    upsert(
        conn,
        event_tickers,
        [{"event_id": event_id, "symbol": s} for s in sorted(set(symbols))],
        key_cols=["event_id", "symbol"],
        update_cols=[],
    )


def write_news_item(
    conn: Connection, item: NewsItem, sources: Sources, now: datetime
) -> WriteResult:
    """Insert or merge one item inside the caller's write transaction."""
    if item.origin not in NEWS_ORIGINS:
        raise ValueError(f"unexpected origin {item.origin!r}")
    canon = canonical_url(item.url)
    h = url_hash(canon)
    host = host_of(canon)
    tier = sources.tier_for(host)
    title = _SPACE_RE.sub(" ", item.title).strip()[:TITLE_MAX] or canon[:TITLE_MAX]
    excerpt = clip_excerpt(item.excerpt)
    t_hash = simhash64(normalize_title(title))
    e_hash = simhash64(excerpt) if excerpt and len(excerpt) >= MIN_EXCERPT_FOR_MATCH else None
    published = to_iso(item.published_at)
    now_iso = to_iso(now)

    # 1. Same URL: an existing event (or an existing source of one).
    existing = conn.execute(select(events.c.id).where(events.c.url_hash == h)).scalar()
    if existing is None:
        existing = conn.execute(
            select(event_sources.c.event_id).where(event_sources.c.url == canon).limit(1)
        ).scalar()
    if existing is not None:
        _add_tickers(conn, existing, item.symbols)
        return WriteResult(existing, created=False, merged=False, syndicated=False)

    source_row = {
        "url": canon,
        "domain": host,
        "trust_tier": tier,
        "title": title,
        "published_at": published,
        "excerpt": excerpt,
        "simhash": u64_to_i64(t_hash),
        "excerpt_simhash": None if e_hash is None else u64_to_i64(e_hash),
        "origin": item.origin,
        "added_at": now_iso,
    }

    # 2. Same story within the window: merge as another source.
    if t_hash:
        lo = to_iso(item.published_at - MERGE_WINDOW)
        hi = to_iso(item.published_at + MERGE_WINDOW)
        candidates = conn.execute(
            select(events.c.id, events.c.simhash)
            .where(
                events.c.origin.in_(NEWS_ORIGINS),
                events.c.simhash.is_not(None),
                events.c.published_at >= lo,
                events.c.published_at <= hi,
            )
            .order_by(events.c.id)
        ).all()
        best: tuple[int, int] | None = None
        for eid, sh in candidates:
            d = hamming(t_hash, i64_to_u64(sh))
            if d <= SIMHASH_MAX_DISTANCE and (best is None or d < best[1]):
                best = (eid, d)
        if best is not None:
            eid = best[0]
            prior = conn.execute(
                select(event_sources.c.excerpt_simhash).where(event_sources.c.event_id == eid)
            ).scalars()
            syndicated = sources.is_syndicator(host) or _excerpt_matches(e_hash, list(prior))
            conn.execute(
                insert(event_sources).values(event_id=eid, syndicated=int(syndicated), **source_row)
            )
            # The story's date is its earliest report.
            conn.execute(
                update(events)
                .where(events.c.id == eid, events.c.published_at > published)
                .values(published_at=published)
            )
            _recount(conn, eid)
            _add_tickers(conn, eid, item.symbols)
            return WriteResult(eid, created=False, merged=True, syndicated=syndicated)

    # 3. A new event.
    event_id: int = conn.execute(
        insert(events)
        .values(
            url_hash=h,
            simhash=u64_to_i64(t_hash) if t_hash else None,
            title=title,
            url=canon,
            source_domain=host,
            trust_tier=tier,
            independent_source_count=1,
            published_at=published,
            excerpt=excerpt,
            origin=item.origin,
            raw=json.dumps(item.raw, sort_keys=True, default=str),
            created_at=now_iso,
        )
        .returning(events.c.id)
    ).scalar_one()
    conn.execute(
        insert(event_sources).values(
            event_id=event_id, syndicated=int(sources.is_syndicator(host)), **source_row
        )
    )
    _add_tickers(conn, event_id, item.symbols)
    return WriteResult(event_id, created=True, merged=False, syndicated=False)


# --------------------------------------------------------------------------- ticker matching


@dataclass(frozen=True)
class Matcher:
    """Matches item text to watchlist tickers (aliases, case-insensitive; symbols, uppercase only)
    and theme keywords. Word-boundary matches only."""

    patterns: tuple[tuple[str, re.Pattern[str]], ...]
    theme: re.Pattern[str] | None

    @classmethod
    def build(
        cls, tickers: Sequence[tuple[str, Sequence[str]]], keywords: Sequence[str]
    ) -> Matcher:
        pats: list[tuple[str, re.Pattern[str]]] = []
        for symbol, aliases in tickers:
            alt = "|".join(re.escape(a) for a in aliases)
            parts = [rf"(?i:{alt})"] if alt else []
            parts.append(rf"\$?{re.escape(symbol)}")  # symbols: case-sensitive
            pats.append(
                (symbol, re.compile(rf"(?<![A-Za-z0-9])(?:{'|'.join(parts)})(?![A-Za-z0-9])"))
            )
        theme = None
        if keywords:
            alt = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
            theme = re.compile(rf"(?i)(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])")
        return cls(tuple(pats), theme)

    def symbols(self, text: str) -> tuple[str, ...]:
        return tuple(s for s, p in self.patterns if p.search(text))

    def is_theme(self, text: str) -> bool:
        return self.theme is not None and self.theme.search(text) is not None
