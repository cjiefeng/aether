"""The monthly universe review run (spec §6.7). Proposals only: Aether never edits the
watchlist; the owner applies a proposal through a PR to `watchlist.yaml`.

    running row (short write)
      → discovery (SEC full-text search, QTUM holdings, current pure-plays)      network, no LLM
      → T1 business excerpts + criteria 1/3/4 (SEC + price provider)              network, no LLM
      → deep research per candidate + the listing sweep (web search)              LLM, run budget
      → evidence rows (short write; their ids are what the proposal cites)
      → proposal (no tools) → validator (retry once) → gates (code wins)          LLM, run budget
      → candidates + done (one short write) → one Telegram message

No write transaction is held across a network or LLM call. Every LLM call carries the run's
`RunBudget` (`UNIVERSE_REVIEW_BUDGET_USD`), outside the daily soft budget. Hitting the cap fails
the run and sends nothing; so does any other failure (the error is stored, the job fails, and
the 2nd-of-month run retries a failed monthly review once).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, insert, select, update

from aether.alerts.candidates import AlertCandidate
from aether.config import OverlayParams, Sources, UniverseConfig, Watchlist
from aether.db.engine import write_tx
from aether.db.models import universe_candidates, universe_evidence, universe_reviews
from aether.db.types import to_iso
from aether.llm.client import BudgetExceeded, LlmClient, LlmError, RunBudget
from aether.portfolio.job import canon
from aether.providers.edgar import EdgarClient
from aether.providers.prices import Bar, PriceProvider, ProviderError
from aether.review.pack import fit
from aether.runs import JobResult
from aether.universe import propose
from aether.universe.business import BusinessExcerpt, fetch_business
from aether.universe.criteria import (
    MIN_PLAUSIBLE_SHARES,
    Criteria,
    Shares,
    evaluate,
    shares_outstanding,
)
from aether.universe.discover import (
    Candidate,
    discover,
    latest_qtum,
    parse_exchange_map,
    parse_fts_hits,
)
from aether.universe.gates import Facts, Gated, c3_streak, gate, is_change
from aether.universe.research import WebEvidence, dossier, sweep
from aether.universe.triggers import structural_triggers

log = logging.getLogger(__name__)

PRICE_LOOKBACK_DAYS = 400
WHERE = "/universe"
DESC_TELEGRAM = 160


class ReviewBusy(RuntimeError):
    pass


@dataclass
class UniverseDeps:
    llm: LlmClient
    edgar: EdgarClient
    prices: PriceProvider
    cfg: UniverseConfig
    sources: Sources
    watchlist: Watchlist
    overlay: OverlayParams
    model: str
    budget_usd: Decimal
    notify: Callable[[Sequence[AlertCandidate]], None]
    # Market-cap fallback when XBRL has no share count (`providers.prices.yfinance_shares`).
    shares_fallback: Callable[[str], int | None] = lambda _symbol: None


@dataclass
class Assessed:
    cand: Candidate
    excerpt: BusinessExcerpt | None = None
    excerpt_note: str | None = None
    criteria: Criteria | None = None
    web: list[WebEvidence] = field(default_factory=list)


# --------------------------------------------------------------------------- DB helpers


def month_done(engine: Engine, month: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(universe_reviews.c.id).where(
                    universe_reviews.c.month == month, universe_reviews.c.status == "done"
                )
            ).first()
            is not None
        )


def _claim(engine: Engine, deps: UniverseDeps, today: date, kind: str, now: datetime) -> int:
    stale = to_iso(now - timedelta(hours=deps.cfg.stale_running_hours))
    with write_tx(engine) as conn:
        conn.execute(
            update(universe_reviews)
            .where(universe_reviews.c.status == "running", universe_reviews.c.created_at < stale)
            .values(status="failed", error="stale: the run never finished", finished_at=to_iso(now))
        )
        if conn.execute(
            select(universe_reviews.c.id).where(universe_reviews.c.status == "running")
        ).first():
            raise ReviewBusy("a universe review is already running")
        rid: int = conn.execute(
            insert(universe_reviews)
            .values(
                as_of=today.isoformat(),
                month=today.strftime("%Y-%m"),
                kind=kind,
                status="running",
                model=deps.model,
                created_at=to_iso(now),
            )
            .returning(universe_reviews.c.id)
        ).scalar_one()
    return rid


def _fail(engine: Engine, rid: int, error: str, cost: Decimal, payload: dict[str, Any]) -> None:
    with write_tx(engine) as conn:
        conn.execute(
            update(universe_reviews)
            .where(universe_reviews.c.id == rid)
            .values(
                status="failed",
                error=error[:1000],
                cost_micros=cost,
                payload=canon(payload),
                finished_at=to_iso(datetime.now(UTC)),
            )
        )


def history(
    engine: Engine, symbols: Sequence[str], before_id: int
) -> dict[str, list[dict[str, Any]]]:
    """Previous done reviews per symbol, newest first: {month, action, criteria}."""
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    if not symbols:
        return out
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                universe_candidates.c.symbol,
                universe_candidates.c.action,
                universe_candidates.c.criteria,
                universe_reviews.c.month,
            )
            .join(universe_reviews, universe_reviews.c.id == universe_candidates.c.review_id)
            .where(
                universe_reviews.c.status == "done",
                universe_reviews.c.id < before_id,
                universe_candidates.c.symbol.in_(list(symbols)),
            )
            .order_by(universe_reviews.c.id.desc())
        ).all()
    for r in rows:
        out[r.symbol].append(
            {"month": r.month, "action": r.action, "criteria": json.loads(r.criteria)}
        )
    return out


# --------------------------------------------------------------------------- steps


def _discover(deps: UniverseDeps, engine: Engine, today: date) -> tuple[Any, str | None]:
    cfg = deps.cfg
    xmap = parse_exchange_map(deps.edgar.company_tickers_exchange())
    start = (today - timedelta(days=round(cfg.fts_lookback_months * 30.44))).isoformat()
    hits: dict[str, dict[str, Any]] = {}
    for q in cfg.fts_queries:
        for h in deps.edgar.full_text_search(q, tuple(cfg.fts_forms), start, today.isoformat()):
            hits.setdefault(str(h.get("_id")), h)
    snap, qtum = latest_qtum(engine)
    found = discover(
        deps.watchlist,
        xmap,
        parse_fts_hits(list(hits.values())),
        qtum,
        exchanges=cfg.exchanges,
        excluded_sics=cfg.excluded_sics,
    )
    found.counts["fts_hits"] = len(hits)
    return found, snap


def _business(deps: UniverseDeps, found: Any, today: date) -> list[Assessed]:
    cfg = deps.cfg
    kept: list[Assessed] = []
    fetched = 0
    for c in found.candidates:
        a = Assessed(c)
        if c.cik is None:
            a.excerpt_note = "no CIK"
        elif fetched >= cfg.max_business_fetches and not c.on_watchlist:
            found.screened.append(
                {"symbol": c.symbol, "name": c.name, "reason": "business-fetch cap reached"}
            )
            continue
        else:
            fetched += 1
            try:
                ex, sic, why = fetch_business(
                    deps.edgar,
                    c.cik,
                    today,
                    scan_chars=cfg.business_scan_chars,
                    excerpt_chars=cfg.business_excerpt_chars,
                    keyword=cfg.business_keyword,
                )
            except (ProviderError, ValueError) as exc:
                ex, sic, why = None, None, (f"SEC fetch failed: {exc}"[:200],)
            a.excerpt = ex
            a.excerpt_note = "; ".join(why) or None
            if not c.on_watchlist and sic and sic in cfg.excluded_sics:
                found.screened.append(
                    {"symbol": c.symbol, "name": c.name, "reason": f"excluded SIC {sic}"}
                )
                continue
        # Pre-filter (no LLM spend): a new company whose business section doesn't mention the
        # keyword (or has none we can find) isn't a pure-play candidate. Current names stay.
        if not c.on_watchlist and not (a.excerpt and a.excerpt.mentions_keyword):
            found.screened.append(
                {
                    "symbol": c.symbol,
                    "name": c.name,
                    "reason": a.excerpt_note
                    or f"business section doesn't mention {cfg.business_keyword!r}",
                }
            )
            continue
        kept.append(a)
    return kept


def _criteria(deps: UniverseDeps, kept: list[Assessed], today: date) -> None:
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


def _research_order(kept: list[Assessed]) -> list[Assessed]:
    def key(a: Assessed) -> tuple[Any, ...]:
        cr = a.criteria
        mcap = int(cr.detail["market_cap"]) if cr and cr.detail.get("market_cap") else 0
        return (
            not a.cand.on_watchlist,
            not (cr and cr.c1),
            not (cr and cr.c3),
            -mcap,
            a.cand.symbol,
        )

    return sorted(kept, key=key)


def _write_evidence(
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


def _propose(
    deps: UniverseDeps,
    budget: RunBudget,
    as_of: str,
    context: str,
    symbols: list[str],
    known: set[str],
) -> propose.Proposal:
    err: str | None = None
    for _ in range(deps.cfg.proposal_max_attempts):
        try:
            msg = deps.llm.complete(
                purpose=propose.PURPOSE,
                model=deps.model,
                system=propose.SYSTEM,
                messages=[{"role": "user", "content": propose.user_message(context, err)}],
                max_tokens=deps.cfg.proposal_max_tokens,
                effort=deps.cfg.proposal_effort,
                output_format=propose.output_format(),
                run_budget=budget,
            )
        except BudgetExceeded:
            raise
        except LlmError as exc:
            raise RuntimeError(f"proposal call failed: {exc}"[:500]) from None
        try:
            return propose.validate(msg, as_of, symbols, known)
        except propose.InvalidProposal as exc:
            err = str(exc)[:300]
            log.warning("universe proposal rejected: %s (%s)", err, propose.prompt_version())
    raise RuntimeError(f"proposal failed validation twice: {err}")


# --------------------------------------------------------------------------- telegram


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def telegram_text(
    as_of: str, rows: Sequence[dict[str, Any]], payload: dict[str, Any], cost: Decimal
) -> str:
    """Plain text, ≤ 4096 chars; anything past the limit says how much more is on /universe."""
    lines = [
        f"Aether universe review · {as_of}",
        "Proposals only: apply one by editing watchlist.yaml in a PR.",
        "",
    ]
    adds = [r for r in rows if r["action"] == "add"]
    removes = [r for r in rows if r["action"] == "remove"]
    watch = [r for r in rows if r["action"] == "watch"]
    if not adds and not removes:
        lines.append("No changes proposed.")
    for title, group in (("Add:", adds), ("Remove:", removes)):
        if group:
            lines.append(title)
            for r in group:
                why = r["gate_note"] or (r["reasons"][0]["text"] if r["reasons"] else "")
                lines.append(f"- {r['symbol']} ({r['name']}): {_clip(why, DESC_TELEGRAM)}")
    if watch:
        lines += ["", "Watch:"]
        for r in watch:
            why = r["gate_note"] or (r["reasons"][0]["text"] if r["reasons"] else "")
            lines.append(f"- {r['symbol']}: {_clip(why, DESC_TELEGRAM)}")
    anns = payload.get("announcements") or []
    if anns:
        lines += ["", f"Announced listings (web, unverified): {len(anns)}"]
    c = payload["counts"]
    lines += [
        "",
        f"Reviewed {c.get('reviewed', 0)} companies ({c.get('screened', 0)} screened out). "
        f"Cost ${cost:.2f}.",
        f"Sources and criteria: {WHERE}. Not financial advice.",
    ]
    return fit(lines, where=WHERE)


# --------------------------------------------------------------------------- run


def run_universe_review(
    engine: Engine,
    deps: UniverseDeps,
    *,
    today: date,
    kind: str = "monthly",
    now: datetime | None = None,
) -> JobResult:
    now = now or datetime.now(UTC)
    month = today.strftime("%Y-%m")
    if kind == "monthly" and month_done(engine, month):
        return JobResult(warning=f"universe review for {month} already done")
    rid = _claim(engine, deps, today, kind, now)
    budget = RunBudget(deps.budget_usd)
    payload: dict[str, Any] = {
        "as_of": today.isoformat(),
        "budget": str(deps.budget_usd),
        "notes": [],
    }
    try:
        found, snap = _discover(deps, engine, today)
        payload["qtum_snapshot"] = snap
        kept = _business(deps, found, today)
        _criteria(deps, kept, today)
        ordered = _research_order(kept)
        chosen = ordered[: deps.cfg.max_research_candidates]
        for a in ordered[deps.cfg.max_research_candidates :]:
            found.screened.append(
                {"symbol": a.cand.symbol, "name": a.cand.name, "reason": "over the research cap"}
            )

        start = today - timedelta(days=deps.cfg.research_lookback_days)
        for a in chosen:
            try:
                a.web = dossier(
                    deps.llm,
                    deps.cfg,
                    deps.sources,
                    deps.model,
                    budget,
                    name=a.cand.name,
                    symbol=a.cand.symbol,
                    start=start,
                    end=today,
                    now=now,
                )
            except BudgetExceeded:
                raise
            except LlmError as exc:
                payload["notes"].append(f"research failed for {a.cand.symbol}: {exc}"[:300])
        swept: list[WebEvidence] = []
        try:
            swept = sweep(
                deps.llm,
                deps.cfg,
                deps.sources,
                deps.model,
                budget,
                start=today - timedelta(days=deps.cfg.fts_lookback_months * 30),
                end=today,
                now=now,
            )
        except BudgetExceeded:
            raise
        except LlmError as exc:
            payload["notes"].append(f"listing sweep failed: {exc}"[:300])

        per, sweep_items = _write_evidence(engine, rid, chosen, swept)
        symbols = [a.cand.symbol for a in chosen]
        hist = history(engine, symbols, rid)
        triggers = structural_triggers(
            engine, [a.cand.symbol for a in chosen if a.cand.on_watchlist], today, deps.overlay
        )
        ctxs: list[propose.CandidateContext] = []
        for a in chosen:
            assert a.criteria is not None
            ctxs.append(
                propose.CandidateContext(
                    symbol=a.cand.symbol,
                    cik=a.cand.cik,
                    sec_name=a.cand.name,
                    tracked=a.cand.on_watchlist,
                    criteria=a.criteria.to_json(),
                    announced=a.cand.announced,
                    triggers=triggers.get(a.cand.symbol, []),
                    history=[f"{h['month']}: {h['action']}" for h in hist[a.cand.symbol][:3]],
                    evidence=per[a.cand.symbol],
                )
            )
        context, known = propose.build_context(today.isoformat(), ctxs, sweep_items)
        proposal = _propose(deps, budget, today.isoformat(), context, symbols, known)

        rows: list[dict[str, Any]] = []
        for a in chosen:
            assert a.criteria is not None
            sym = a.cand.symbol
            entry = proposal.entries[sym]
            cited = propose.cited_ids(entry)
            excerpt_ref = next((e.ref for e in per[sym] if e.kind == "business_excerpt"), None)
            crit = a.criteria
            facts = Facts(
                tracked=a.cand.on_watchlist,
                c1=crit.c1,
                c3=crit.c3,
                c4=crit.c4,
                announced=a.cand.announced,
                excerpt_ref=excerpt_ref,
                c3_fails=[
                    f for f in crit.detail["fails"] if not f.startswith(("not listed", "history"))
                ],
                c4_sessions=int(crit.detail["sessions"]),
                min_sessions=deps.cfg.min_sessions,
                structural=triggers.get(sym, []),
                c3_streak=c3_streak(
                    not crit.c3 and not crit.detail["c3_unknown"],
                    [h["criteria"] for h in hist[sym]],
                    deps.cfg.remove_after_failed_reviews,
                ),
            )
            g: Gated = gate(facts, entry["action"], cited)
            criteria_json = {
                **crit.to_json(),
                "c2_excerpt": excerpt_ref,
                "c2_note": a.excerpt_note,
                "announced": a.cand.announced,
                "sources": sorted(a.cand.sources),
                "structural": list(facts.structural),
                "c3_streak": facts.c3_streak,
            }
            rows.append(
                {
                    "symbol": sym,
                    "track": "pure_play",
                    "action": g.action,
                    "proposed_action": g.proposed,
                    "cik": a.cand.cik,
                    "name": entry["name"],
                    "overlap": canon(
                        {"qtum_weight_pct": a.cand.qtum_weight, "qtum_snapshot": snap}
                    ),
                    "criteria": canon(criteria_json),
                    "description": entry["description"],
                    "reasons": entry["reasons"],
                    "evidence_ids": [
                        int(i[1:])
                        for i in dict.fromkeys(cited + ([excerpt_ref] if excerpt_ref else []))
                    ],
                    "gate_note": g.note,
                }
            )
        found.counts["reviewed"] = len(chosen)
        found.counts["screened"] = len(found.screened)
        payload.update(
            {
                "counts": found.counts,
                "screened": found.screened,
                "announcements": proposal.announcements,
                "changes": sum(is_change(r["action"]) for r in rows),
                "prompt_version": propose.prompt_version(),
            }
        )
        text = telegram_text(today.isoformat(), rows, payload, budget.spent)
    except Exception as exc:
        reason = f"budget cap reached: {exc}" if isinstance(exc, BudgetExceeded) else repr(exc)
        _fail(engine, rid, reason, budget.spent, payload)
        if isinstance(exc, BudgetExceeded):
            raise RuntimeError(f"universe review stopped at the run cap: {exc}"[:500]) from None
        raise

    with write_tx(engine) as conn:
        if rows:
            conn.execute(
                insert(universe_candidates),
                [
                    {
                        **r,
                        "review_id": rid,
                        "reasons": canon(r["reasons"]),
                        "evidence_ids": canon(r["evidence_ids"]),
                    }
                    for r in rows
                ],
            )
        conn.execute(
            update(universe_reviews)
            .where(universe_reviews.c.id == rid)
            .values(
                status="done",
                payload=canon(payload),
                prompt_version=propose.prompt_version(),
                cost_micros=budget.spent,
                telegram_text=text,
                finished_at=to_iso(datetime.now(UTC)),
            )
        )
    key = f"universe_review:{month}" if kind == "monthly" else f"universe_review:manual:{rid}"
    deps.notify(
        [
            AlertCandidate(
                kind="universe_review", dedupe_key=key, text=text, payload={"review_id": rid}
            )
        ]
    )
    return JobResult(
        rows_written=len(rows),
        provider=deps.model,
        warning="; ".join(payload["notes"]) or None,
    )
