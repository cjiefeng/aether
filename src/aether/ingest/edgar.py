"""SEC EDGAR ingest for the pure-plays (spec §4, §5.2): filings, Form 4 insider transactions,
lock-ups, ATM/shelf capacity, going-concern language, XBRL fundamentals and capital structure,
and the deterministic RISK events.

1. Network first, outside any transaction: submissions per CIK; then documents only for new
   filings that an extractor needs (Form 4 XML, final prospectuses, 424B5/424B2 supplements,
   10-K/10-Q); then companyfacts for symbols with a new periodic report.
2. One `write_tx` upserts everything. Re-runs are idempotent: filings key on accession, events on
   the filing URL hash, and a document is fetched once (`filings.parsed` records the result).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from aether.classify.rules import RuleHit, classify_filing
from aether.config import Rubric
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    capital_structure,
    event_classifications,
    event_sources,
    event_tickers,
    events,
    filings,
    fundamentals_q,
    insider_txns,
    lockups,
    tickers,
    xbrl_fetches,
)
from aether.db.types import utcnow_iso
from aether.edgar import xbrl
from aether.edgar.form4 import Form4Error, InsiderTxn, parse_form4
from aether.edgar.submissions import FilingMeta, iter_filings, older_pages
from aether.edgar.text import (
    extract_atm,
    extract_delisted_class,
    extract_going_concern,
    extract_listing_notice,
    extract_lockup,
    extract_offering,
    html_to_text,
)
from aether.providers.edgar import EdgarClient
from aether.providers.prices import ProviderError
from aether.runs import JobResult

log = logging.getLogger(__name__)

SINCE = date(2025, 1, 1)
MAX_DOCS_PER_RUN = 400

FORM4_FORMS = frozenset({"4", "4/A"})
LOCKUP_FORMS = frozenset({"424B4", "424B1"})
ATM_FORMS = frozenset({"424B5", "424B2"})
# M13: primary prospectuses whose cover size feeds the 10%-of-FD-shares dilution escalation.
OFFERING_FORMS = LOCKUP_FORMS | ATM_FORMS
PERIODIC_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})
SHELF_FORMS = frozenset({"S-3", "S-3ASR", "F-3", "F-3ASR"})
# M5 overlay: removal/deregistration notices (which security class?) and 8-K Item 3.01 bodies
# (deficiency or voluntary transfer?).
DELISTING_FORMS = frozenset({"25", "25-NSE", "15-12B", "15-12G"})
EIGHT_K_FORMS = frozenset({"8-K", "8-K/A"})
DOC_FORMS = FORM4_FORMS | LOCKUP_FORMS | ATM_FORMS | PERIODIC_FORMS | DELISTING_FORMS


def wants_document(f: FilingMeta) -> bool:
    return f.form in DOC_FORMS or (f.form in EIGHT_K_FORMS and "3.01" in f.items)


SEC_DOMAIN = "sec.gov"


def url_hash(url: str) -> bytes:
    return hashlib.sha256(url.encode("utf-8")).digest()


@dataclass
class ParsedDoc:
    """Extractor results for one filing document (stored as `filings.parsed`)."""

    summary: dict[str, Any] = field(default_factory=dict)
    txns: tuple[InsiderTxn, ...] = ()
    lockup_row: dict[str, Any] | None = None
    atm_row: dict[str, Any] | None = None
    going_concern_excerpt: str | None = None
    lockup_excerpt: str | None = None


def parse_document(symbol: str, f: FilingMeta, body: str) -> ParsedDoc:
    """Run the extractors that apply to this form. Pure: no I/O."""
    if f.form in FORM4_FORMS:
        try:
            doc = parse_form4(body)
        except Form4Error as exc:
            return ParsedDoc(summary={"error": str(exc)[:200]})
        return ParsedDoc(
            summary={"form4_txns": len(doc.txns), "aff10b5One": doc.aff_10b5_1},
            txns=doc.txns,
        )

    if f.form in DELISTING_FORMS:
        cls = extract_delisted_class(body)
        return ParsedDoc(
            summary={
                "security": None if cls is None else cls.title,
                "covers_common": None if cls is None else cls.covers_common,
            }
        )

    text = html_to_text(body)
    out = ParsedDoc(summary={"chars": len(text)})
    if f.form in EIGHT_K_FORMS and "3.01" in f.items:
        notice = extract_listing_notice(text)
        out.summary["listing_notice"] = notice.kind
        out.summary["listing_excerpt"] = notice.excerpt
    if f.form in LOCKUP_FORMS:
        lk = extract_lockup(text)
        out.summary["lockup"] = (
            None
            if lk is None
            else {
                "days": lk.days,
                "prospectus_date": lk.prospectus_date.isoformat(),
                "expiry": lk.expiry.isoformat(),
            }
        )
        if lk is not None:
            out.lockup_excerpt = lk.excerpt
            out.lockup_row = {
                "accession": f.accession,
                "symbol": symbol,
                "prospectus_date": lk.prospectus_date.isoformat(),
                "lockup_days": lk.days,
                "expiry_date": lk.expiry.isoformat(),
                "early_release_possible": int(lk.early_release_possible),
                "excerpt": lk.excerpt,
            }
    if f.form in ATM_FORMS:
        atm = extract_atm(text)
        out.summary["atm"] = None if atm is None else {"amount": str(atm.amount)}
        if atm is not None:
            out.atm_row = {
                "symbol": symbol,
                "as_of": f.filed_at,
                "instrument": "atm",
                "source_accession": f.accession,
                "amount_micros": atm.amount,
                "shares_underlying": None,
                "strike_micros": None,
                "source": "filing_text",
                "concept": None,
                "excerpt": atm.excerpt,
            }
    if f.form in OFFERING_FORMS:
        off = extract_offering(text)
        out.summary["offering"] = (
            None if off is None else {"shares": off.shares, "prefunded": off.prefunded}
        )
    if f.form in PERIODIC_FORMS:
        gc = extract_going_concern(text)
        out.summary["going_concern"] = gc is not None
        if gc is not None:
            out.going_concern_excerpt = gc.excerpt
    return out


# --------------------------------------------------------------------------- reads


def pure_play_ciks(engine: Engine, symbols: Sequence[str] | None = None) -> list[tuple[str, str]]:
    q = select(tickers.c.symbol, tickers.c.cik).where(
        tickers.c.active == 1, tickers.c.type == "pure_play", tickers.c.cik.is_not(None)
    )
    if symbols is not None:
        q = q.where(tickers.c.symbol.in_(list(symbols)))
    with engine.connect() as conn:
        return [(s, c) for s, c in conn.execute(q.order_by(tickers.c.symbol)).all()]


def parsed_accessions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(select(filings.c.accession).where(filings.c.parsed.is_not(None))).scalars()
        )


def known_accessions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(select(filings.c.accession)).scalars())


def symbols_with_current_xbrl(engine: Engine) -> set[str]:
    """Symbols whose companyfacts was fetched with the current parser version."""
    with engine.connect() as conn:
        return set(
            conn.execute(
                select(xbrl_fetches.c.symbol).where(
                    xbrl_fetches.c.parser_version == xbrl.PARSER_VERSION
                )
            ).scalars()
        )


# --------------------------------------------------------------------------- network


def fetch_filings(client: EdgarClient, cik: str, since: date) -> list[FilingMeta]:
    subs = client.submissions(cik)
    metas = list(iter_filings(subs.get("filings", {}).get("recent", {}), cik))
    for page in older_pages(subs, since):
        metas += list(iter_filings(client.submissions_page(page), cik))
    return [m for m in metas if m.filed_at >= since.isoformat()]


# --------------------------------------------------------------------------- rows


def filing_row(symbol: str, f: FilingMeta, fetched_at: str) -> dict[str, Any]:
    return {
        "accession": f.accession,
        "symbol": symbol,
        "cik": f.cik,
        "form": f.form,
        "filed_at": f.filed_at,
        "accepted_at": f.accepted_at,
        "report_date": f.report_date,
        "items": json.dumps(list(f.items)),
        "primary_doc": f.primary_doc,
        "primary_doc_description": f.primary_doc_description,
        "url": f.url,
        "is_xbrl": int(f.is_xbrl),
        "fetched_at": fetched_at,
    }


def txn_rows(symbol: str, accession: str, txns: Sequence[InsiderTxn]) -> list[dict[str, Any]]:
    return [
        {
            "accession": accession,
            "seq": t.seq,
            "symbol": symbol,
            "insider_cik": t.insider_cik,
            "insider": t.insider,
            "role": t.role,
            "security": t.security,
            "txn_date": t.txn_date,
            "code": t.code,
            "acquired_disposed": t.acquired_disposed,
            "shares": t.shares,
            "price": t.price,
            "is_10b5_1": int(t.is_10b5_1),
            "is_derivative": int(t.is_derivative),
        }
        for t in txns
    ]


def fundamental_rows(symbol: str, facts: Sequence[xbrl.XbrlFact]) -> list[dict[str, Any]]:
    rows = []
    for f in facts:
        money = f.unit == "USD"
        rows.append(
            {
                "symbol": symbol,
                "period_end": f.period_end,
                "concept": f.concept,
                "period_days": f.period_days,
                "value_micros": f.value if money else None,
                "value_int": None if money else int(f.value),
                "unit": f.unit,
                "fy": f.fy,
                "fp": f.fp,
                "form": f.form,
                "accession": f.accession,
                "filed": f.filed,
            }
        )
    return rows


def xbrl_capital_rows(symbol: str, items: Sequence[xbrl.XbrlInstrument]) -> list[dict[str, Any]]:
    return [
        {
            "symbol": symbol,
            "as_of": i.as_of,
            "instrument": i.instrument,
            "source_accession": i.accession,
            "amount_micros": i.amount,
            "shares_underlying": i.shares_underlying,
            "strike_micros": i.strike,
            "source": "xbrl",
            "concept": i.concept,
            "excerpt": None,
        }
        for i in items
    ]


def shelf_row(symbol: str, f: FilingMeta) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "as_of": f.filed_at,
        "instrument": "shelf",
        "source_accession": f.accession,
        "amount_micros": None,
        "shares_underlying": None,
        "strike_micros": None,
        "source": "form",
        "concept": None,
        "excerpt": None,
    }


def write_event(conn: Connection, symbol: str, f: FilingMeta, hit: RuleHit, now: str) -> int:
    """Insert-or-update one EDGAR event and its rule classification. Returns the event id."""
    h = url_hash(f.url)
    upsert(
        conn,
        events,
        [
            {
                "url_hash": h,
                "title": hit.title[:300],
                "url": f.url,
                "source_domain": SEC_DOMAIN,
                "trust_tier": "T1",
                "independent_source_count": 1,
                "published_at": f.accepted_at or f"{f.filed_at}T00:00:00Z",
                "excerpt": hit.evidence_quote,
                "origin": "edgar",
                "accession": f.accession,
                "raw": json.dumps({"form": f.form, "items": list(f.items)}),
                "created_at": now,
            }
        ],
        key_cols=["url_hash"],
        update_cols=["title", "excerpt", "raw"],
    )
    event_id: int = conn.execute(select(events.c.id).where(events.c.url_hash == h)).scalar_one()
    upsert(
        conn,
        event_sources,
        [{"event_id": event_id, "url": f.url, "domain": SEC_DOMAIN, "trust_tier": "T1"}],
        key_cols=["event_id", "url"],
    )
    upsert(
        conn,
        event_tickers,
        [{"event_id": event_id, "symbol": symbol}],
        key_cols=["event_id", "symbol"],
    )
    upsert(
        conn,
        event_classifications,
        [
            {
                "event_id": event_id,
                "class": hit.cls,
                "category": hit.category,
                "materiality_raw": hit.materiality,
                "materiality": hit.materiality,  # T1: no trust-tier cap applies
                "direction": hit.direction,
                "confidence": hit.confidence,
                "rationale": hit.rationale,
                "evidence_quote": hit.evidence_quote,
                "rule_id": hit.rule_id,
                "model": None,
                "prompt_version": None,
                "created_at": now,
            }
        ],
        key_cols=["event_id"],
    )
    return event_id


# --------------------------------------------------------------------------- job


def ingest_edgar(
    engine: Engine,
    client: EdgarClient,
    rubric: Rubric,
    *,
    symbols: Sequence[str] | None = None,
    since: date = SINCE,
    max_docs: int = MAX_DOCS_PER_RUN,
) -> JobResult:
    targets = pure_play_ciks(engine, symbols)
    done = parsed_accessions(engine)
    known = known_accessions(engine)
    have_fundamentals = symbols_with_current_xbrl(engine)

    # 1) Network, outside any transaction.
    errors: list[str] = []
    metas: dict[str, list[FilingMeta]] = {}
    for sym, cik in targets:
        try:
            metas[sym] = fetch_filings(client, cik, since)
        except ProviderError as exc:
            log.warning("edgar submissions %s failed: %s", sym, exc)
            errors.append(f"{sym}: {exc}")
    if targets and len(errors) == len(targets):
        raise ProviderError("all symbols failed: " + " | ".join(errors))

    todo = sorted(
        (
            (sym, f)
            for sym, fs in metas.items()
            for f in fs
            if wants_document(f) and f.primary_doc and f.accession not in done
        ),
        key=lambda x: x[1].filed_at,
        reverse=True,
    )
    if len(todo) > max_docs:
        log.info("edgar: %d documents pending; fetching %d this run", len(todo), max_docs)
        todo = todo[:max_docs]
    parsed: dict[str, ParsedDoc] = {}
    for sym, f in todo:
        assert f.primary_doc is not None
        try:
            body = client.document(
                f.cik, f.accession, f.primary_doc, raw=f.form in FORM4_FORMS | DELISTING_FORMS
            )
        except ProviderError as exc:
            log.warning("edgar document %s %s failed: %s", sym, f.accession, exc)
            errors.append(f"{sym} {f.accession}: {exc}")
            continue
        parsed[f.accession] = parse_document(sym, f, body)

    xbrl_data: dict[str, tuple[list[xbrl.XbrlFact], list[xbrl.XbrlInstrument]]] = {}
    for sym, cik in targets:
        new_periodic = any(
            f.form in PERIODIC_FORMS and f.accession not in known for f in metas.get(sym, [])
        )
        if sym not in metas or not (new_periodic or sym not in have_fundamentals):
            continue
        try:
            cf = client.companyfacts(cik)
        except ProviderError as exc:
            log.warning("edgar companyfacts %s failed: %s", sym, exc)
            errors.append(f"{sym} companyfacts: {exc}")
            continue
        xbrl_data[sym] = (xbrl.fundamentals(cf), xbrl.capital_structure(cf))

    # 2) One short write transaction.
    now = utcnow_iso()
    written = 0
    with write_tx(engine) as conn:
        for sym, fs in metas.items():
            rows = [filing_row(sym, f, now) for f in fs]
            # `parsed` is never overwritten here; it is set below only for new results.
            written += upsert(conn, filings, rows, key_cols=["accession"])
            for f in fs:
                p = parsed.get(f.accession)
                if p is not None:
                    conn.execute(
                        update(filings)
                        .where(filings.c.accession == f.accession)
                        .values(parsed=json.dumps(p.summary))
                    )
                    written += upsert(
                        conn,
                        insider_txns,
                        txn_rows(sym, f.accession, p.txns),
                        key_cols=["accession", "seq"],
                    )
                    if p.lockup_row:
                        written += upsert(conn, lockups, [p.lockup_row], key_cols=["accession"])
                    if p.atm_row:
                        written += upsert(
                            conn,
                            capital_structure,
                            [p.atm_row],
                            key_cols=["symbol", "as_of", "instrument", "source_accession"],
                        )
                if f.form in SHELF_FORMS:
                    written += upsert(
                        conn,
                        capital_structure,
                        [shelf_row(sym, f)],
                        key_cols=["symbol", "as_of", "instrument", "source_accession"],
                    )
                # Classify on first sight (form/item rules need no document) and again when a
                # document was parsed this run (Form 4 sales, going concern, lock-up excerpt).
                # A failed document fetch never hides a form-based RISK event.
                if f.accession in known and p is None:
                    continue
                hit = classify_filing(
                    sym,
                    f,
                    rubric,
                    txns=p.txns if p else (),
                    going_concern_excerpt=p.going_concern_excerpt if p else None,
                    lockup_excerpt=p.lockup_excerpt if p else None,
                )
                if hit is not None:
                    write_event(conn, sym, f, hit, now)
                    written += 1
        for sym, (facts, instruments) in xbrl_data.items():
            written += upsert(
                conn,
                fundamentals_q,
                fundamental_rows(sym, facts),
                key_cols=["symbol", "period_end", "concept", "period_days"],
            )
            written += upsert(
                conn,
                capital_structure,
                xbrl_capital_rows(sym, instruments),
                key_cols=["symbol", "as_of", "instrument", "source_accession"],
            )
            upsert(
                conn,
                xbrl_fetches,
                [{"symbol": sym, "parser_version": xbrl.PARSER_VERSION, "fetched_at": now}],
                key_cols=["symbol"],
            )

    log.info(
        "edgar: %d filings, %d documents parsed, %d SEC requests",
        sum(len(v) for v in metas.values()),
        len(parsed),
        client.requests_made,
    )
    return JobResult(
        rows_written=written,
        provider="sec",
        warning=("; ".join(errors))[:500] if errors else None,
    )
