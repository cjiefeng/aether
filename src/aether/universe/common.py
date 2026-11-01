"""Helpers shared by the universe review's tracks (M12 pure-play, M14 adjacent, M14 full)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, insert

from aether.db.engine import write_tx
from aether.db.models import universe_evidence
from aether.portfolio import thesis
from aether.providers.prices import Bar, ProviderError
from aether.universe import candidate_checks, propose
from aether.universe.business import BusinessExcerpt
from aether.universe.criteria import (
    MIN_PLAUSIBLE_SHARES,
    Criteria,
    Shares,
    evaluate,
    shares_outstanding,
)
from aether.universe.discover import Candidate
from aether.universe.research import WebEvidence

if TYPE_CHECKING:
    from aether.universe.run import UniverseDeps

log = logging.getLogger(__name__)

PRICE_LOOKBACK_DAYS = 400


@dataclass
class Assessed:
    cand: Candidate
    excerpt: BusinessExcerpt | None = None
    excerpt_note: str | None = None
    criteria: Criteria | None = None
    web: list[WebEvidence] = field(default_factory=list)


def assess_criteria(deps: UniverseDeps, kept: list[Assessed], today: date) -> None:
    for a in kept:
        c = a.cand
        shares = None
        if c.cik is not None:
            try:
                shares = shares_outstanding(deps.edgar.companyfacts(c.cik))
            except (ProviderError, ValueError):
                shares = None
        if shares is None and c.listing is not None:
            n = deps.shares_fallback(c.listing.ticker)
            if n is not None and n >= MIN_PLAUSIBLE_SHARES:
                shares = Shares(n, today.isoformat(), "", "yfinance")
        bars: list[Bar] = []
        err = None
        if c.listing is not None:
            try:
                bars = deps.prices.fetch_daily(
                    c.listing.ticker, today - timedelta(days=PRICE_LOOKBACK_DAYS), today
                )
            except ProviderError as exc:
                err = str(exc)
        a.criteria = evaluate(
            deps.cfg,
            exchange=c.listing.exchange if c.listing else None,
            ticker=c.listing.ticker if c.listing else None,
            cik=c.cik,
            bars=bars,
            shares=shares,
            today=today,
            price_error=err,
        )


def candidate_red_flags(deps: UniverseDeps, a: Assessed, today: date) -> dict[str, Any] | None:
    """M18 (§6.7.4): the four red flags for an untracked candidate that passed the deterministic
    criteria 1, 3 and 4 (the caller adds its track's evidence rule). None when not applicable.
    Network inside; call with no transaction open."""
    c, crit = a.cand, a.criteria
    if c.on_watchlist or c.cik is None or crit is None or not (crit.c1 and crit.c3 and crit.c4):
        return None
    fs = crit.detail.get("first_session")
    try:
        return candidate_checks.run(
            deps.edgar,
            c.symbol,
            c.cik,
            as_of=today,
            first_session=date.fromisoformat(fs) if fs else None,
            cfg=deps.cfg,
            thesis_cfg=deps.thesis,
        )
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        # Unexpected data: every check is unknown, with the reason; the review goes on.
        log.warning("candidate red flags %s failed: %r", c.symbol, exc)
        flags = thesis.red_flags(thesis.NameInputs(c.symbol), deps.thesis)
        return {
            "symbol": c.symbol,
            "as_of": today.isoformat(),
            "fpi": False,
            "checks": flags,
            "n_flags": 0,
            "n_unknown": len(flags),
            "resale_overhang": [],
            "instances": [],
            "requests": 0,
            "errors": [f"checks failed: {exc!r}"[:200]],
        }


def write_evidence(
    engine: Engine, rid: int, chosen: list[Assessed], swept: list[WebEvidence]
) -> tuple[dict[str, list[propose.EvidenceItem]], list[propose.EvidenceItem]]:
    """One short write; returns the evidence with its ids, per symbol and for the sweep."""
    per: dict[str, list[propose.EvidenceItem]] = {a.cand.symbol: [] for a in chosen}
    sweep_items: list[propose.EvidenceItem] = []
    with write_tx(engine) as conn:

        def put(row: dict[str, Any]) -> int:
            return int(
                conn.execute(
                    insert(universe_evidence)
                    .values(review_id=rid, **row)
                    .returning(universe_evidence.c.id)
                ).scalar_one()
            )

        for a in chosen:
            ex = a.excerpt
            if ex is not None:
                eid = put(
                    {
                        "symbol": a.cand.symbol,
                        "kind": "business_excerpt",
                        "url": ex.url,
                        "domain": "sec.gov",
                        "trust_tier": "T1",
                        "title": f"{a.cand.name}: {ex.form} filed {ex.filed}, business section"[
                            :500
                        ],
                        "excerpt": ex.excerpt,
                        "published_at": ex.filed,
                        "date_source": "filing",
                        "form": ex.form,
                        "accession": ex.accession,
                    }
                )
                per[a.cand.symbol].append(
                    propose.EvidenceItem(
                        eid,
                        a.cand.symbol,
                        "business_excerpt",
                        "T1",
                        "sec.gov",
                        a.cand.name,
                        ex.excerpt,
                        ex.filed,
                        ex.form,
                    )
                )
            for w in a.web:
                per[a.cand.symbol].append(_web_item(put(_web_row(w)), w))
        for w in swept:
            sweep_items.append(_web_item(put(_web_row(w)), w))
    return per, sweep_items


def _web_row(w: WebEvidence) -> dict[str, Any]:
    return {
        "symbol": w.symbol,
        "kind": "web",
        "url": w.url,
        "domain": w.domain,
        "trust_tier": w.trust_tier,
        "title": w.title,
        "excerpt": w.excerpt,
        "published_at": w.published_at,
        "date_source": w.date_source,
    }


def _web_item(eid: int, w: WebEvidence) -> propose.EvidenceItem:
    published = None if w.date_source == "retrieved" else w.published_at[:10]
    return propose.EvidenceItem(
        eid, w.symbol, "web", w.trust_tier, w.domain, w.title, w.excerpt, published
    )
