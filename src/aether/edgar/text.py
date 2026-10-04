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
