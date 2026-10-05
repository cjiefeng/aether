"""RSS 2.0 / Atom feeds (M6): fetch with robots.txt + conditional GET, parse with the stdlib.

- https only (redirects included), an identifying User-Agent, a response size cap and a timeout.
- robots.txt is read once per host per run. An explicit `Disallow` for the feed path skips the feed.
  If robots.txt is missing (4xx) or can't be fetched, the feed is still read: a published feed is an
  invitation to subscribe, and some IR hosts time out on robots.txt (decision recorded in the M6
  report).
- XML with a DTD or entity declaration is rejected (no XXE / billion laughs), as in providers/fx.py.

Feed text is untrusted (S1); this module only extracts fields.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from aether.providers.prices import ProviderError

USER_AGENT = "aether-news/0.1 (personal research; RSS reader)"
MAX_BYTES = 5_000_000
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
ROBOTS_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
_DTD_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
DC = "{http://purl.org/dc/elements/1.1/}"


@dataclass(frozen=True)
class FeedEntry:
    title: str
    link: str
    published: datetime | None  # timezone-aware UTC, None if missing/unparseable
    summary: str  # raw (often HTML) description; untrusted


@dataclass(frozen=True)
class FeedResponse:
    status: int  # 200, or 304 (not modified)
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None
    notes: tuple[str, ...] = field(default=())


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    try:
        dt = parsedate_to_datetime(v)  # RFC 822 (RSS)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))  # RFC 3339 (Atom)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return None  # a date without a zone is ambiguous; treat as unknown
    return dt.astimezone(UTC)


def _text(el: ET.Element | None) -> str:
    return "" if el is None else "".join(el.itertext()).strip()


def parse_feed(xml_text: str, base_url: str) -> list[FeedEntry]:
    if _DTD_RE.search(xml_text):
        raise ProviderError("rss: DTD/entity declarations are not accepted")
    try:
        root = ET.fromstring(xml_text.lstrip("﻿"))  # noqa: S314 - DTDs rejected above
    except ET.ParseError as exc:
        raise ProviderError(f"rss: malformed XML: {exc}") from exc
    out: list[FeedEntry] = []
    if root.tag == f"{ATOM}feed":
        for e in root.iter(f"{ATOM}entry"):
            href = ""
            for ln in e.findall(f"{ATOM}link"):
                if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                    href = ln.get("href", "")
                    break
            published = parse_date(_text(e.find(f"{ATOM}published"))) or parse_date(
                _text(e.find(f"{ATOM}updated"))
            )
            summary = _text(e.find(f"{ATOM}summary")) or _text(e.find(f"{ATOM}content"))
            out.append(
                FeedEntry(
                    _text(e.find(f"{ATOM}title")), urljoin(base_url, href), published, summary
                )
            )
        return out
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ProviderError(f"rss: unsupported feed root <{root.tag}>")
    for item in channel.iter("item"):
        link = _text(item.find("link"))
        if not link:
            guid = item.find("guid")
            if guid is not None and guid.get("isPermaLink", "true") != "false":
                link = _text(guid)
        published = parse_date(_text(item.find("pubDate"))) or parse_date(
            _text(item.find(f"{DC}date"))
        )
        summary = _text(item.find("description")) or _text(item.find(f"{CONTENT}encoded"))
        out.append(
            FeedEntry(_text(item.find("title")), urljoin(base_url, link), published, summary)
        )
    return out


class RssClient:
    """One per run: holds the HTTP client and the per-host robots.txt cache."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        self._owns = client is None
        self._robots: dict[str, RobotFileParser | None] = {}

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def _robots_for(self, url: str) -> tuple[RobotFileParser | None, str | None]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin], None
        note = None
        parser: RobotFileParser | None = None
        try:
            r = self._client.get(
                origin + "/robots.txt", timeout=ROBOTS_TIMEOUT, headers={"User-Agent": USER_AGENT}
            )
            if r.status_code == 200:
                parser = RobotFileParser()
                parser.parse(r.text.splitlines())
            elif r.status_code >= 500:
                note = f"robots.txt HTTP {r.status_code} at {parts.netloc}; feed read anyway"
        except httpx.HTTPError as exc:
            note = (
                f"robots.txt unreachable at {parts.netloc} ({type(exc).__name__}); feed read anyway"
            )
        self._robots[origin] = parser
        return parser, note

    def fetch(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> FeedResponse:
        if urlsplit(url).scheme != "https":
            raise ProviderError("rss: only https feeds are fetched")
        robots, note = self._robots_for(url)
        notes = (note,) if note else ()
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            raise ProviderError("rss: disallowed by robots.txt")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            with self._client.stream("GET", url, headers=headers) as r:
                if urlsplit(str(r.url)).scheme != "https":
                    raise ProviderError("rss: redirected off https")
                if r.status_code == 304:
                    return FeedResponse(304, etag=etag, last_modified=last_modified, notes=notes)
                if r.status_code != 200:
                    raise ProviderError(f"rss: HTTP {r.status_code}")
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise ProviderError("rss: response larger than 5 MB")
                text = body.decode(r.encoding or "utf-8", errors="replace")
                return FeedResponse(
                    200,
                    text=text,
                    etag=r.headers.get("etag"),
                    last_modified=r.headers.get("last-modified"),
                    notes=notes,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"rss: {type(exc).__name__}") from exc
