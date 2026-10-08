"""Filing HTML -> plain text, plus deterministic extractors (no LLM in Phase 1).

Each extractor returns `None` when its pattern is absent: nothing is guessed. Matches carry a
short excerpt (<= 600 chars, S6) so the dashboard can show the evidence next to the value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

EXCERPT_MAX = 600

_BLOCK = {
    "p",
    "div",
    "br",
    "tr",
    "li",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "td",
    "th",
}
_SKIP = {"script", "style", "ix:header", "head"}


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


_QUOTES = str.maketrans(  # curly quotes and no-break space -> ASCII
    {0x2019: "'", 0x2018: "'", 0x201C: '"', 0x201D: '"', 0xA0: " "}
)


def html_to_text(html: str) -> str:
    p = _TextParser()
    p.feed(html)
    p.close()
    return " ".join("".join(p.parts).translate(_QUOTES).split())


def excerpt(text: str, start: int, end: int, limit: int = EXCERPT_MAX) -> str:
    """A window around [start, end) of at most `limit` chars, trimmed to word boundaries."""
    pad = max(0, (limit - (end - start)) // 2)
    a, b = max(0, start - pad), min(len(text), end + pad)
    out = text[a:b]
    if a > 0:
        out = "…" + out.split(" ", 1)[-1]
    if b < len(text):
        out = out.rsplit(" ", 1)[0] + "…"
    return out[:limit]


# --------------------------------------------------------------------------- lock-up

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_PROSPECTUS_DATE = re.compile(rf"Prospectus dated ({_MONTHS}) (\d{{1,2}}), (\d{{4}})")
_LOCKUP_DAYS = re.compile(
    r"(?:period|ending|for)\s+(?:of\s+)?(\d{2,3})\s+days\s+after\s+the\s+date\s+of\s+this\s+"
    r"prospectus",
    re.IGNORECASE,
)
_LOCKUP_CONTEXT = re.compile(r"lock-?up|restricted period", re.IGNORECASE)
_EARLY_RELEASE = re.compile(
    r"(?:may|can)[^.]{0,40}(?:in their sole discretion|at any time)[^.]{0,80}release",
    re.IGNORECASE,
)
CONTEXT_WINDOW = 1500


@dataclass(frozen=True)
class Lockup:
    prospectus_date: date
    days: int
    expiry: date
    early_release_possible: bool
    excerpt: str


def prospectus_date(text: str) -> date | None:
    m = _PROSPECTUS_DATE.search(text)
    if not m:
        return None
    return datetime.strptime(f"{m[1]} {m[2]} {m[3]}", "%B %d %Y").date()


def extract_lockup(text: str) -> Lockup | None:
    """Lock-up length from a final prospectus (424B4/424B1): "N days after the date of this
    prospectus" near "lock-up"/"restricted period". The most frequent N wins (ties: longer)."""
    pdate = prospectus_date(text)
    if pdate is None:
        return None
    hits: dict[int, list[re.Match[str]]] = {}
    for m in _LOCKUP_DAYS.finditer(text):
        lo, hi = max(0, m.start() - CONTEXT_WINDOW), m.end() + CONTEXT_WINDOW
        if _LOCKUP_CONTEXT.search(text, lo, hi):
            hits.setdefault(int(m[1]), []).append(m)
    if not hits:
        return None
    days = max(hits, key=lambda n: (len(hits[n]), n))
    m = hits[days][0]
    return Lockup(
        prospectus_date=pdate,
        days=days,
        expiry=pdate + timedelta(days=days),
        early_release_possible=_EARLY_RELEASE.search(text) is not None,
        excerpt=excerpt(text, m.start(), m.end()),
    )


# --------------------------------------------------------------------------- going concern

_SENTENCE = re.compile(r"[^.]*substantial doubt[^.]*going concern[^.]*\.", re.IGNORECASE)
# Risk-factor boilerplate is hypothetical ("could raise substantial doubt") and MD&A often looks
# back ("we disclosed that there was substantial doubt ..."); only an unhedged, present-tense
# statement counts. Conservative by design: when in doubt, no flag.
_HEDGE = re.compile(
    r"\b(could|may|might|would|if|unless|whether|no|not|alleviat\w*|absent|in the event|"
    r"disclosed|previously|prior|had been|was)\b",
    re.IGNORECASE,
)
# A later "... has been alleviated" / "no longer exists" cancels an earlier statement.
_RESOLVED = re.compile(r"alleviat\w*|no longer (?:exists|present)", re.IGNORECASE)
RESOLUTION_WINDOW = 2000


@dataclass(frozen=True)
class GoingConcern:
    excerpt: str


def extract_going_concern(text: str) -> GoingConcern | None:
    for m in _SENTENCE.finditer(text):
        if _HEDGE.search(m[0]) or _RESOLVED.search(text, m.end(), m.end() + RESOLUTION_WINDOW):
            continue
        return GoingConcern(excerpt=excerpt(text, m.start(), m.end()))
    return None


# --------------------------------------------------------------------------- at-the-market

_ATM = re.compile(r"at[- ]the[- ]market", re.IGNORECASE)
_AGG = re.compile(
    r"aggregate (?:gross )?(?:offering|sales) price of up to \$\s?([\d,]+(?:\.\d+)?)"
    r"(?:\s*(million|billion))?",
    re.IGNORECASE,
)
_SCALE = {None: Decimal(1), "million": Decimal(10) ** 6, "billion": Decimal(10) ** 9}


@dataclass(frozen=True)
class AtmProgram:
    amount: Decimal  # USD
    excerpt: str


ATM_WINDOW = 3000


def extract_atm(text: str) -> AtmProgram | None:
    """An at-the-market program with a stated size: "aggregate offering price of up to $X" within
    `ATM_WINDOW` chars of "at-the-market". Base-prospectus boilerplate that merely allows ATM
    sales (no amount nearby) does not count."""
    for m in _AGG.finditer(text):
        lo, hi = max(0, m.start() - ATM_WINDOW), m.end() + ATM_WINDOW
        if not _ATM.search(text, lo, hi):
            continue
        try:
            amount = Decimal(m[1].replace(",", "")) * _SCALE[(m[2] or "").lower() or None]
        except InvalidOperation:
            continue
        return AtmProgram(amount=amount, excerpt=excerpt(text, m.start(), m.end()))
    return None


# --------------------------------------------------------------------------- offering size (M13)

# The base offering on a prospectus cover: "We are offering N shares of [our] [Class A] common
# stock", plus pre-funded warrants offered in the same sentence (they are shares in all but
# name). The underwriters' option and accompanying common warrants are left out: the size is a
# floor, so the M13 dilution escalation can only under-fire. Only the cover area is searched.
_OFFERING = re.compile(
    r"\bwe\s+are\s+offering\s+(?:an\s+aggregate\s+of\s+|up\s+to\s+)?(?:\(i\)\s*)?"
    r"(\d{1,3}(?:,\d{3})+|\d{4,})\s+shares\s+of\s+(?:our\s+)?(?:Class\s+[A-Z]\s+)?"
    r"common\s+stock",
    re.IGNORECASE,
)
_PREFUNDED = re.compile(
    r"pre-?funded\s+warrants\s+to\s+purchase\s+(?:up\s+to\s+)?(?:an\s+aggregate\s+of\s+)?"
    r"(\d{1,3}(?:,\d{3})+|\d{4,})\s+shares",
    re.IGNORECASE,
)
COVER_CHARS = 20_000
PREFUNDED_WINDOW = 600


@dataclass(frozen=True)
class Offering:
    shares: int  # common shares offered, plus pre-funded warrant shares in the same offer
    prefunded: int
    excerpt: str


def extract_offering(text: str) -> Offering | None:
    cover = text[:COVER_CHARS]
    m = _OFFERING.search(cover)
    if not m:
        return None
    shares = int(m[1].replace(",", ""))
    pf = _PREFUNDED.search(cover, m.end(), m.end() + PREFUNDED_WINDOW)
    prefunded = int(pf[1].replace(",", "")) if pf else 0
    end = pf.end() if pf else m.end()
    return Offering(shares + prefunded, prefunded, excerpt(text, m.start(), end))


# --------------------------------------------------------------------------- listing (M5 overlay)

# 8-K Item 3.01 covers both deficiency notices and voluntary exchange transfers. Only a clear
# deficiency with no transfer language counts as a "deficiency"; conservative by design.
_ITEM_301 = re.compile(r"Item\s*3\.01", re.IGNORECASE)
# The item's standard caption mentions both a failure to satisfy a rule and a transfer; skip it.
_CAPTION_301 = re.compile(
    r"Item\s*3\.01\.?\s*Notice\s+of\s+Delisting\s+or\s+Failure\s+to\s+Satisfy\s+a\s+Continued"
    r"\s+Listing\s+Rule\s+or\s+Standard;?\s*Transfer\s+of\s+Listing\.?",
    re.IGNORECASE,
)
_TRANSFER = re.compile(
    r"transfer(?:ring)?\s+(?:of\s+)?(?:the\s+|its\s+)?(?:stock\s+exchange\s+)?listing"
    r"|transfer\s+(?:its|the)\s+(?:common\s+stock|class\s+a|listing|securities)"
    r"|voluntar\w+\s+(?:to\s+)?(?:transfer|delist)",
    re.IGNORECASE,
)
_DEFICIENCY = re.compile(
    r"not\s+in\s+compliance|no\s+longer\s+(?:in\s+compliance|compl\w+)|regain\s+compliance"
    r"|failure\s+to\s+(?:satisfy|comply|meet)|deficiency|minimum\s+bid\s+price"
    r"|delisting\s+determination|staff\s+determination|determined\s+to\s+(?:delist|commence)"
    r"|non-?compliance|suspend\w*\s+trading",
    re.IGNORECASE,
)
ITEM_WINDOW = 4000


@dataclass(frozen=True)
class ListingNotice:
    kind: str  # deficiency | transfer | ambiguous | unclear
    excerpt: str


def extract_listing_notice(text: str) -> ListingNotice:
    cap = _CAPTION_301.search(text)
    m = cap or _ITEM_301.search(text)
    start = m.end() if m else 0
    window = text[start : start + ITEM_WINDOW]
    nxt = re.search(r"Item\s*\d\.\d\d", window)  # stop at the next item
    if nxt:
        window = window[: nxt.start()]
    transfer, deficiency = _TRANSFER.search(window), _DEFICIENCY.search(window)
    if deficiency and not transfer:
        kind, hit = "deficiency", deficiency
    elif transfer and not deficiency:
        kind, hit = "transfer", transfer
    elif transfer and deficiency:
        kind, hit = "ambiguous", deficiency
    else:
        return ListingNotice("unclear", excerpt(text, start, start + 300))
    return ListingNotice(kind, excerpt(text, start + hit.start(), start + hit.end()))


# Form 25 / 25-NSE / 15-12B / 15-12G: which class of securities is removed or deregistered.
_CLASS_XML = re.compile(
    r"<descriptionClassSecurity>(.*?)</descriptionClassSecurity>", re.IGNORECASE | re.DOTALL
)
# Form 25 (HTML): the title(s) precede "(Description of class of securities)".
_CLASS_25 = re.compile(r"\)\s*_*\s*([^()]{3,400}?)\s*\(Description of class of securities\)", re.I)
# Form 15: "Title of each class of securities covered by this Form: ...".
_CLASS_15 = re.compile(r"Title of (?:each )?class of securities[^:]*:?\s*(.{0,300})", re.I)
_COMMON = re.compile(r"common\s+stock|ordinary\s+shares?|common\s+shares?", re.IGNORECASE)
# Phrases that mention common stock only as what another security converts into or contains.
_DERIVED = re.compile(
    r"(?:exercisable|convertible|exchangeable)\s+(?:for|into)[^,;]*|consisting\s+of[^,;]*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DelistedClass:
    title: str
    covers_common: bool


def extract_delisted_class(body: str) -> DelistedClass | None:
    m = _CLASS_XML.search(body)
    if m:
        title = " ".join(m.group(1).split())
    else:
        text = html_to_text(body)
        t = _CLASS_25.search(text) or _CLASS_15.search(text)
        if not t:
            return None
        title = " ".join(t.group(1).split()).strip(" _")
    covers = bool(_COMMON.search(_DERIVED.sub(" ", title)))
    return DelistedClass(title[:300], covers)
