"""Criterion 2 evidence (spec §6.7): a ≤600-char excerpt from the business section of the
candidate's latest 10-K / 20-F / S-1 / F-1 / S-4 / 424B4 on EDGAR (a T1 source).

Deterministic, no LLM. The section is found by its heading; table-of-contents entries (a heading
followed closely by the next item's heading) are skipped. When no section is found there is no
excerpt, and the candidate can't be an `add` (fail closed). The excerpt is untrusted filing text:
it reaches the proposal only inside `wrap_untrusted`.

The `quantum` keyword check over the first `business_scan_chars` is the review's pre-filter: a
new candidate whose business section doesn't mention it is screened out before any LLM spend.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from aether.edgar.text import html_to_text
from aether.providers.edgar import EdgarClient, filing_folder

ANNUAL_FORMS = ("10-K", "20-F", "10-K/A", "20-F/A")
OFFERING_FORMS = ("424B4", "S-1", "F-1", "S-4", "S-1/A", "F-1/A", "S-4/A")
ANNUAL_MAX_AGE = timedelta(days=460)  # a 10-K/20-F older than ~15 months is stale
TOC_GAP = 400  # a heading followed by another heading within this many chars is a TOC entry
MIN_SECTION = 600

_ITEM1 = re.compile(r"\bItem\s*1\s*[.:\-\u2013\u2014]?\s*Business\b", re.IGNORECASE)
_ITEM1_END = re.compile(r"\bItem\s*1A\b|\bItem\s*2\b", re.IGNORECASE)
_ITEM4 = re.compile(
    r"\bItem\s*4\s*[.:\-\u2013\u2014]?\s*Information\s+on\s+the\s+Company\b", re.IGNORECASE
)
_ITEM4_END = re.compile(r"\bItem\s*4A\b|\bItem\s*5\b", re.IGNORECASE)
_20F_OVERVIEW = re.compile(r"\bB\.\s*Business\s+Overview\b", re.IGNORECASE)
_20F_OVERVIEW_END = re.compile(r"\bC\.\s*Organizational\s+Structure\b", re.IGNORECASE)
# Prospectuses: the BUSINESS section (an all-caps heading), else the PROSPECTUS SUMMARY.
_PROS_BUSINESS = re.compile(r"\b(?:BUSINESS|Business of [A-Z][\w.,&' ]{1,60})\b")
_PROS_SUMMARY = re.compile(r"\bPROSPECTUS SUMMARY\b")
_PROS_END = re.compile(r"\b(?:RISK FACTORS|MANAGEMENT|USE OF PROCEEDS)\b")


@dataclass(frozen=True)
class FilingRef:
    form: str
    filed: str
    accession: str
    primary_doc: str

    def url(self, cik: str) -> str:
        return f"{filing_folder(cik, self.accession)}/{self.primary_doc}"


@dataclass(frozen=True)
class BusinessExcerpt:
    form: str
    filed: str
    accession: str
    url: str
    excerpt: str  # first ≤ N chars of the section
    mentions_keyword: bool  # keyword within the first business_scan_chars
    sic: str | None


def pick_filing(submissions: Mapping[str, Any], today: date) -> FilingRef | None:
    """The latest 10-K/20-F if it's recent, else the latest prospectus-type filing."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    rows = [
        FilingRef(str(f), str(d), str(a), str(p))
        for f, d, a, p in zip(forms, dates, accs, docs, strict=False)
        if p
    ]
    rows.sort(key=lambda r: (r.filed, r.accession), reverse=True)
    cutoff = (today - ANNUAL_MAX_AGE).isoformat()
    for r in rows:
        if r.form in ANNUAL_FORMS and r.filed >= cutoff:
            return r
    for r in rows:
        if r.form in OFFERING_FORMS:
            return r
    for r in rows:
        if r.form in ANNUAL_FORMS:
            return r
    return None


def _section(text: str, start: re.Pattern[str], end: re.Pattern[str]) -> str | None:
    """Text after the first heading match that isn't a table-of-contents entry."""
    for m in start.finditer(text):
        body = text[m.end() :]
        nxt = end.search(body)
        if nxt is not None and nxt.start() < TOC_GAP:
            continue  # TOC line: "Item 1. Business 4 Item 1A. Risk Factors 20"
        stop = nxt.start() if nxt is not None else len(body)
        section = re.sub(r"^[\s.:\-\u2013\u2014\d]+", "", body[:stop]).strip()
        if len(section) >= MIN_SECTION:
            return section
    return None


def business_section(text: str, form: str) -> str | None:
    base = form.split("/")[0]
    if base == "10-K":
        return _section(text, _ITEM1, _ITEM1_END)
    if base == "20-F":
        return _section(text, _20F_OVERVIEW, _20F_OVERVIEW_END) or _section(
            text, _ITEM4, _ITEM4_END
        )
    return _section(text, _PROS_BUSINESS, _PROS_END) or _section(text, _PROS_SUMMARY, _PROS_END)


def clip_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut + "…"


def business_excerpt(section: str, limit: int, keyword: str, scan_chars: int) -> str:
    """The section's opening; if the opening doesn't mention the keyword but the scan window
    does, a window from the start of the sentence that first mentions it."""
    head = clip_words(section, limit)
    low = section[:scan_chars].lower()
    i = low.find(keyword.lower())
    if i < 0 or keyword.lower() in head.lower():
        return head
    start = max(section.rfind(". ", 0, i) + 2, 0, i - limit // 2)
    return "…" + clip_words(section[start:], limit - 1)


def extract(
    html: str,
    ref: FilingRef,
    cik: str,
    *,
    scan_chars: int,
    excerpt_chars: int,
    keyword: str,
    sic: str | None,
) -> BusinessExcerpt | None:
    text = html_to_text(html)
    section = business_section(text, ref.form)
    if section is None:
        return None
    return BusinessExcerpt(
        form=ref.form,
        filed=ref.filed,
        accession=ref.accession,
        url=ref.url(cik),
        excerpt=business_excerpt(section, excerpt_chars, keyword, scan_chars),
        mentions_keyword=keyword.lower() in section[:scan_chars].lower(),
        sic=sic,
    )


def fetch_business(
    client: EdgarClient,
    cik: str,
    today: date,
    *,
    scan_chars: int,
    excerpt_chars: int,
    keyword: str,
) -> tuple[BusinessExcerpt | None, str | None, Sequence[str]]:
    """(excerpt or None, SIC code, reason when None). Raises `ProviderError` on SEC failures."""
    subs = client.submissions(cik)
    sic = str(subs.get("sic") or "") or None
    ref = pick_filing(subs, today)
    if ref is None:
        return None, sic, ("no 10-K/20-F/S-1/F-1/S-4/424B4 on EDGAR",)
    html = client.document(cik, ref.accession, ref.primary_doc)
    ex = extract(
        html,
        ref,
        cik,
        scan_chars=scan_chars,
        excerpt_chars=excerpt_chars,
        keyword=keyword,
        sic=sic,
    )
    if ex is None:
        return None, sic, (f"business section not found in {ref.form} {ref.accession}",)
    return ex, sic, ()
