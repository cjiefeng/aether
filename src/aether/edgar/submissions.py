"""Parse SEC `submissions` JSON (`filings.recent` and the older `files[]` pages) into rows."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from aether.providers.edgar import ACCESSION_RE, DOC_RE, index_url

COLUMNS = (
    "accessionNumber",
    "filingDate",
    "acceptanceDateTime",
    "reportDate",
    "form",
    "items",
    "primaryDocument",
    "primaryDocDescription",
    "isXBRL",
)


@dataclass(frozen=True)
class FilingMeta:
    accession: str
    cik: str
    form: str
    filed_at: str  # YYYY-MM-DD
    accepted_at: str | None  # UTC ISO, second precision
    report_date: str | None
    items: tuple[str, ...]
    primary_doc: str | None
    primary_doc_description: str | None
    is_xbrl: bool

    @property
    def url(self) -> str:
        return index_url(self.cik, self.accession)


def _accepted(v: object) -> str | None:
    # "2026-09-08T16:05:12.000Z" -> "2026-09-08T16:05:12Z"
    if not isinstance(v, str) or len(v) < 19:
        return None
    return v[:19] + "Z"


def _opt(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def iter_filings(block: Mapping[str, Any], cik: str) -> Iterator[FilingMeta]:
    """`block` is `filings.recent` or a `files[]` page: parallel arrays keyed by column."""
    cols = {c: block.get(c) or [] for c in COLUMNS}
    n = len(cols["accessionNumber"])
    for i in range(n):

        def at(c: str, i: int = i) -> object:
            arr = cols[c]
            return arr[i] if i < len(arr) else None

        acc, filed = at("accessionNumber"), at("filingDate")
        if not isinstance(acc, str) or not ACCESSION_RE.fullmatch(acc):
            continue
        if not isinstance(filed, str):
            continue
        try:
            date.fromisoformat(filed)
        except ValueError:
            continue
        doc = _opt(at("primaryDocument"))
        if doc is not None and (not DOC_RE.fullmatch(doc) or ".." in doc):
            doc = None
        items_raw = at("items")
        items = tuple(
            x.strip() for x in (items_raw if isinstance(items_raw, str) else "").split(",")
        )
        yield FilingMeta(
            accession=acc,
            cik=cik,
            form=str(at("form") or "").strip(),
            filed_at=filed,
            accepted_at=_accepted(at("acceptanceDateTime")),
            report_date=_opt(at("reportDate")),
            items=tuple(x for x in items if x),
            primary_doc=doc,
            primary_doc_description=_opt(at("primaryDocDescription")),
            is_xbrl=bool(at("isXBRL")),
        )


def older_pages(submissions: Mapping[str, Any], since: date) -> list[str]:
    """Names of `files[]` pages that may contain filings on/after `since`."""
    out = []
    for f in submissions.get("filings", {}).get("files", []) or []:
        name, to = f.get("name"), f.get("filingTo")
        if isinstance(name, str) and isinstance(to, str) and to >= since.isoformat():
            out.append(name)
    return out
