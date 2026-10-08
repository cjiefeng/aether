"""The adjacent-industry track of the monthly review (spec §6.7.1, M14). Proposals only.

    discovery: sector seeds + current adjacent names (SEC exchange map)            no LLM
      → hard exclusions in code, before any research: hyperscalers / cloud platforms
        (`excluded_symbols`), semiconductors (SEC SIC 3674), pure-plays, other watchlist names;
        names without a NYSE/Nasdaq listing are "outside mandate"
      → T1 business excerpt (the first "quantum" mention in the latest 10-K's business section)
      → criteria 1/3/4 (listing, size and liquidity, history) in code
      → one dossier per candidate + one industry sweep (web search)                LLM, own cap
      → evidence rows (short write)
      → proposal (no tools) → validator (retry once) → `gates.gate_adjacent`        LLM, own cap

The track has its own `RunBudget` (`UNIVERSE_ADJACENT_BUDGET_USD`). Hitting it, or any track
failure, stops **only this track**: its result is `failed` with the reason and no partial rows;
the pure-play track still completes. Slot rules, strong candidates and the shortlist are applied
afterwards across both tracks (`slots.py`).

Qualifying evidence (add eligibility, strong candidates): a T1 or T2 item dated in the last 12
months whose title or excerpt mentions "quantum". Undated web results never qualify.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine

from aether.config import AdjacentTrack, Watchlist
from aether.llm.client import BudgetExceeded, LlmError, RunBudget
from aether.providers.prices import ProviderError
from aether.universe import gates, propose, propose_adjacent
from aether.universe.business import fetch_business
from aether.universe.common import Assessed, assess_criteria, write_evidence
from aether.universe.discover import Candidate, ExchangeMap
from aether.universe.gates import AdjFacts, c3_streak
from aether.universe.research import WebEvidence, adjacent_dossier, adjacent_sweep
from aether.universe.slots import Trigger
from aether.universe.triggers import structural_triggers

if TYPE_CHECKING:
    from aether.universe.run import UniverseDeps

log = logging.getLogger(__name__)

KEYWORD = "quantum"
OUTSIDE = "outside mandate"
NOT_INVESTABLE = "not investable"


@dataclass
class TrackResult:
    status: str = "done"  # done | failed | skipped
    error: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, list[propose.EvidenceItem]] = field(default_factory=dict)
    screened: list[dict[str, Any]] = field(default_factory=list)
    info: list[dict[str, Any]] = field(default_factory=list)  # not investable / outside mandate
    notes: list[str] = field(default_factory=list)
    cost: Decimal = Decimal(0)
    prompt_version: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "screened": self.screened,
            "info": self.info,
            "notes": self.notes,
            "cost": str(self.cost),
            "prompt_version": self.prompt_version,
            "reviewed": len(self.rows),
        }


# --------------------------------------------------------------------------- discovery


@dataclass
class Discovered:
    cands: list[Candidate]
    sectors: dict[str, str]
    screened: list[dict[str, Any]]
    outside: list[dict[str, Any]]
    excluded_tracked: dict[str, str]  # tracked name -> why it's now in an excluded category


def discover_adjacent(
    watchlist: Watchlist,
    xmap: ExchangeMap,
    adj: AdjacentTrack,
    exchanges: Sequence[str],
    qtum: Mapping[str, float],
) -> Discovered:
    tracked = {t.symbol: t for t in watchlist.tickers if t.type == "adjacent" and t.active}
    other_types = {t.symbol: t.type for t in watchlist.tickers if t.symbol not in tracked}
    order: list[tuple[str, str]] = [(s, t.sector or "") for s, t in tracked.items()]
    for sid, sec in sorted(adj.sectors.items(), key=lambda kv: (kv[1].priority, kv[0])):
        order += [(s, sid) for s in sec.seeds]
    seen: set[str] = set()
    out = Discovered([], {}, [], [], {})
    for sym, sector in order:
        if sym in seen:
            continue
        seen.add(sym)
        is_tracked = sym in tracked
        if sym in adj.excluded_symbols:
            if is_tracked:
                out.excluded_tracked[sym] = "excluded symbol (hyperscaler / cloud platform)"
            else:
                out.screened.append(
                    {
                        "symbol": sym,
                        "sector": sector,
                        "reason": "excluded: hyperscaler / cloud platform",
                    }
                )
                continue
        if not is_tracked and sym in other_types:
            typ = other_types[sym]
            reason = (
                "pure-play: reviewed in the pure-play track"
                if typ == "pure_play"
                else f"already on the watchlist ({typ})"
            )
            out.screened.append({"symbol": sym, "sector": sector, "reason": reason})
            continue
        x = xmap.by_ticker.get(sym)
        listing = xmap.listing(x.cik, exchanges) if x is not None else None
        if listing is None and not is_tracked:
            out.outside.append(
                {
                    "symbol": sym,
                    "sector": sector,
                    "status": OUTSIDE,
                    "reason": "no NYSE/Nasdaq listing in SEC's exchange map",
                }
            )
            continue
        cik = x.cik if x is not None else tracked[sym].cik
        out.cands.append(
            Candidate(
                symbol=sym,
                cik=cik,
                name=listing.name if listing else (x.name if x else sym),
                sources={"watchlist"} if is_tracked else {"seed"},
                listing=listing,
                on_watchlist=is_tracked,
                qtum_weight=qtum.get(sym),
            )
        )
        out.sectors[sym] = sector
    return out


# --------------------------------------------------------------------------- evidence rules


def qualifies(e: propose.EvidenceItem, as_of: date, days: int) -> bool:
    if e.trust_tier not in ("T1", "T2") or not e.published:
        return False
    if e.published < (as_of - timedelta(days=days)).isoformat():
        return False
    return KEYWORD in f"{e.title} {e.excerpt or ''}".lower()


def evidence_counts(
    items: Sequence[propose.EvidenceItem], as_of: date, days: int
) -> tuple[int, int, frozenset[str], str | None]:
    """(T1 items, independent T2 domains, refs of T1 quantum items, newest qualifying date)."""
    q = [e for e in items if qualifies(e, as_of, days)]
    t1 = [e for e in q if e.trust_tier == "T1"]
    t2 = {e.domain for e in q if e.trust_tier == "T2"}
    latest = max((e.published for e in q if e.published), default=None)
    return len(t1), len(t2), frozenset(e.ref for e in t1), latest


def _crit_line(c: Mapping[str, Any], bucket: str | None) -> str:
    def usd(key: str) -> str:
        v = c.get(key)
        return f"${int(v):,}" if v else "unknown"

    return (
        f"listing {'NYSE/Nasdaq' if c.get('c1') else 'none'} ({c.get('exchange') or 'none'}); "
        f"market cap {usd('market_cap')} (bucket {bucket or 'unknown'}); median dollar volume "
        f"{usd('median_dollar_volume')}; floors {'met' if c.get('c3') else 'not met'}; "
        f"{c.get('sessions', 0)} sessions ({'enough' if c.get('c4') else 'too few'})"
    )


# --------------------------------------------------------------------------- run


def run_adjacent_track(
    engine: Engine,
    deps: UniverseDeps,
    rid: int,
    *,
    today: date,
    now: datetime,
    xmap: ExchangeMap,
    qtum: Mapping[str, float],
    snap: str | None,
    triggers: Mapping[str, Sequence[Trigger]],
    current_lines: Sequence[str],
    history: Callable[[Sequence[str]], Mapping[str, list[dict[str, Any]]]],
    fills_gap: Callable[[str], bool],
    budget: RunBudget,
) -> TrackResult:
    res = TrackResult()
    adj = deps.cfg.adjacent
    if adj is None:
        res.status = "skipped"
        res.error = "no adjacent block in universe.yaml"
        return res
    try:
        _run(
            engine,
            deps,
            adj,
            rid,
            res,
            today,
            now,
            xmap,
            qtum,
            snap,
            triggers,
            current_lines,
            history,
            fills_gap,
            budget,
        )
    except (BudgetExceeded, LlmError, ProviderError, RuntimeError, ValueError) as exc:
        res.status = "failed"
        res.error = (
            f"budget cap reached: {exc}" if isinstance(exc, BudgetExceeded) else repr(exc)
        )[:500]
        res.rows = []
        log.warning("adjacent track failed: %s", res.error)
    res.cost = budget.spent
    return res


def _run(
    engine: Engine,
    deps: UniverseDeps,
    adj: AdjacentTrack,
    rid: int,
    res: TrackResult,
    today: date,
    now: datetime,
    xmap: ExchangeMap,
    qtum: Mapping[str, float],
    snap: str | None,
    triggers: Mapping[str, Sequence[Trigger]],
    current_lines: Sequence[str],
    history: Callable[[Sequence[str]], Mapping[str, list[dict[str, Any]]]],
    fills_gap: Callable[[str], bool],
    budget: RunBudget,
) -> None:
    cfg = deps.cfg
    found = discover_adjacent(deps.watchlist, xmap, adj, cfg.exchanges, qtum)
    res.screened += found.screened
    res.info += found.outside

    # T1 excerpt + SIC exclusion (before any research).
    kept: list[Assessed] = []
    excluded_tracked = dict(found.excluded_tracked)
    for c in found.cands:
        a = Assessed(c)
        if c.cik is None:
            a.excerpt_note = "no CIK"
            kept.append(a)
            continue
        try:
            ex, sic, why = fetch_business(
                deps.edgar,
                c.cik,
                today,
                scan_chars=adj.business_scan_chars,
                excerpt_chars=cfg.business_excerpt_chars,
                keyword=KEYWORD,
            )
        except (ProviderError, ValueError) as exc:
            ex, sic, why = None, None, (f"SEC fetch failed: {exc}"[:200],)
        if sic and sic in adj.excluded_sics:
            if c.on_watchlist:
                excluded_tracked[c.symbol] = f"SEC SIC {sic} (semiconductors)"
            else:
                res.screened.append(
                    {
                        "symbol": c.symbol,
                        "sector": found.sectors.get(c.symbol),
                        "reason": f"excluded: SEC SIC {sic} (semiconductors)",
                    }
                )
                continue
        # Only an excerpt that mentions quantum is quantum-related evidence; otherwise it's kept
        # out (the opening of a diversified company's 10-K says nothing about the track).
        a.excerpt = ex if ex is not None and ex.mentions_keyword else None
        a.excerpt_note = "; ".join(why) or (
            None if a.excerpt else f"the business section doesn't mention {KEYWORD!r}"
        )
        kept.append(a)
    assess_criteria(deps, kept, today)

    def order(a: Assessed) -> tuple[Any, ...]:
        cr = a.criteria
        mcap = int(cr.detail["market_cap"]) if cr and cr.detail.get("market_cap") else 0
        sec = adj.sectors.get(found.sectors.get(a.cand.symbol, ""))  # type: ignore[call-overload]
        return (not a.cand.on_watchlist, sec.priority if sec else 9, -mcap, a.cand.symbol)

    ordered = sorted(kept, key=order)
    chosen = ordered[: adj.max_research_candidates]
    for a in ordered[adj.max_research_candidates :]:
        res.screened.append(
            {
                "symbol": a.cand.symbol,
                "sector": found.sectors.get(a.cand.symbol),
                "reason": "over the research cap",
            }
        )

    if not chosen:
        res.notes.append("no adjacent candidates to review")
        return
    start = today - timedelta(days=adj.evidence_lookback_days)
    researched: set[str] = set()
    for a in chosen:
        try:
            a.web = adjacent_dossier(
                deps.llm,
                cfg,
                deps.sources,
                deps.model,
                budget,
                name=a.cand.name,
                symbol=a.cand.symbol,
                sector=found.sectors.get(a.cand.symbol, ""),
                start=start,
                end=today,
                now=now,
            )
            researched.add(a.cand.symbol)
        except BudgetExceeded:
            raise
        except LlmError as exc:
            res.notes.append(f"research failed for {a.cand.symbol}: {exc}"[:300])
    swept: list[WebEvidence] = []
    try:
        swept = adjacent_sweep(
            deps.llm,
            cfg,
            deps.sources,
            deps.model,
            budget,
            sectors=sorted(adj.sectors),
            start=start,
            end=today,
            now=now,
        )
    except BudgetExceeded:
        raise
    except LlmError as exc:
        res.notes.append(f"industry sweep failed: {exc}"[:300])

    per, sweep_items = write_evidence(engine, rid, chosen, swept)
    res.evidence = per
    symbols = [a.cand.symbol for a in chosen]
    hist = history(symbols)
    tracked = [a.cand.symbol for a in chosen if a.cand.on_watchlist]
    struct = structural_triggers(engine, tracked, today, deps.overlay)

    ctxs: list[propose_adjacent.AdjacentContext] = []
    facts_by: dict[str, AdjFacts] = {}
    extra: dict[str, dict[str, Any]] = {}
    for a in chosen:
        assert a.criteria is not None
        sym = a.cand.symbol
        crit = a.criteria
        mcap = Decimal(crit.detail["market_cap"]) if crit.detail.get("market_cap") else None
        bucket = cfg.mcap_buckets.bucket(mcap)
        t1, t2, t1_refs, latest = evidence_counts(per[sym], today, adj.evidence_lookback_days)
        sector = found.sectors.get(sym, "")
        structural = list(struct.get(sym, []))
        if a.cand.on_watchlist:
            if sym in excluded_tracked:
                structural.append(
                    f"reclassified into an excluded category: {excluded_tracked[sym]}"
                )
            if a.cand.listing is None:
                structural.append("no NYSE/Nasdaq listing in SEC's exchange map (delisted?)")
            if sym in researched and t1 == 0 and t2 == 0:
                structural.append("no quantum-related evidence (T1 or T2) in the last 12 months")
        qual = {t.ref: t.text for t in triggers.get(sym, [])}
        streak = c3_streak(
            not crit.c3 and not crit.detail["c3_unknown"],
            [h["criteria"] for h in hist.get(sym, [])],
            cfg.remove_after_failed_reviews,
        )
        facts_by[sym] = AdjFacts(
            tracked=a.cand.on_watchlist,
            c1=crit.c1,
            c3=crit.c3,
            c4=crit.c4,
            c3_fails=[
                f for f in crit.detail["fails"] if not f.startswith(("not listed", "history"))
            ],
            c4_sessions=int(crit.detail["sessions"]),
            min_sessions=cfg.min_sessions,
            structural=structural,
            qualifying=qual,
            t1_items=t1,
            t2_domains=t2,
            min_t2=cfg.removal.min_independent_t2,
            t1_quantum_refs=t1_refs,
            c3_streak=streak if a.cand.on_watchlist else False,
        )
        gap = fills_gap(sector)
        extra[sym] = {
            "mcap": mcap,
            "bucket": bucket,
            "sector": sector,
            "fills_gap": gap,
            "evidence_t1": t1,
            "evidence_t2": t2,
            "evidence_independent": len({e.domain for e in per[sym] if e.ref in t1_refs}) + t2,
            "evidence_latest": latest,
        }
        ctxs.append(
            propose_adjacent.AdjacentContext(
                symbol=sym,
                cik=a.cand.cik,
                sec_name=a.cand.name,
                sector=sector,
                tracked=a.cand.on_watchlist,
                criteria_line=_crit_line(crit.to_json(), bucket),
                qtum_weight=a.cand.qtum_weight,
                fills_gap=gap,
                triggers=[*structural, *(t.line() for t in triggers.get(sym, []))],
                history=[f"{h['month']}: {h['action']}" for h in hist.get(sym, [])[:3]],
                evidence=per[sym],
            )
        )
    trigger_refs = {t.ref for ts in triggers.values() for t in ts}
    context, known = propose_adjacent.build_context(
        today.isoformat(), ctxs, sweep_items, current_lines, trigger_refs
    )
    active = {t.symbol for t in deps.watchlist.sleeve()}
    proposal = _propose(deps, budget, today.isoformat(), context, symbols, known, active)
    res.prompt_version = propose_adjacent.prompt_version()

    for o in proposal.others:
        status = {"private": NOT_INVESTABLE, "non_us": OUTSIDE}.get(
            o["status"], "listed, not reviewed"
        )
        res.info.append(
            {
                "name": o["name"],
                "status": status,
                "description": o["description"],
                "evidence_ids": o["evidence_ids"],
            }
        )

    for a in chosen:
        assert a.criteria is not None
        sym = a.cand.symbol
        entry = proposal.entries[sym]
        cited = propose.cited_ids(entry)
        g = gates.gate_adjacent(facts_by[sym], entry, cited)
        x = extra[sym]
        crit = a.criteria
        wc = entry.get("weakest_current") or {}
        criteria_obj = {
            **crit.to_json(),
            "tracked": a.cand.on_watchlist,
            "c2_excerpt": next((e.ref for e in per[sym] if e.kind == "business_excerpt"), None),
            "c2_note": a.excerpt_note,
            "structural": list(facts_by[sym].structural),
            "qualifying": dict(facts_by[sym].qualifying),
            "c3_streak": facts_by[sym].c3_streak,
            "fills_gap": x["fills_gap"],
            "evidence_t1": x["evidence_t1"],
            "evidence_t2": x["evidence_t2"],
            "evidence_independent": x["evidence_independent"],
            "evidence_latest": x["evidence_latest"],
            "proposed_exposure": entry.get("exposure"),
            "exposure_note": g.exposure_note,
            "market_cap_note": entry.get("market_cap_note") or None,
            "weakest_current": wc if wc.get("symbol") else None,
            "sources": sorted(a.cand.sources),
        }
        res.rows.append(
            {
                "symbol": sym,
                "track": "adjacent",
                "action": g.action,
                "proposed_action": g.proposed,
                "cik": a.cand.cik,
                "name": entry["name"],
                "overlap": {"qtum_weight_pct": a.cand.qtum_weight, "qtum_snapshot": snap},
                "criteria_obj": criteria_obj,
                "description": entry["description"],
                "reasons": entry["reasons"],
                "evidence_ids": [int(i[1:]) for i in dict.fromkeys(cited) if i.startswith("U")],
                "gate_note": g.note,
                "sector": x["sector"] or None,
                "exposure": g.exposure,
                "market_cap_micros": x["mcap"],
                "mcap_bucket": x["bucket"],
            }
        )


def _propose(
    deps: UniverseDeps,
    budget: RunBudget,
    as_of: str,
    context: str,
    symbols: list[str],
    known: set[str],
    active: set[str],
) -> propose_adjacent.AdjacentProposal:
    err: str | None = None
    for _ in range(deps.cfg.proposal_max_attempts):
        try:
            msg = deps.llm.complete(
                purpose=propose_adjacent.PURPOSE,
                model=deps.model,
                system=propose_adjacent.SYSTEM,
                messages=[{"role": "user", "content": propose_adjacent.user_message(context, err)}],
                max_tokens=deps.cfg.proposal_max_tokens,
                effort=deps.cfg.proposal_effort,
                output_format=propose_adjacent.output_format(),
                run_budget=budget,
            )
        except BudgetExceeded:
            raise
        except LlmError as exc:
            raise RuntimeError(f"adjacent proposal call failed: {exc}"[:500]) from None
        try:
            return propose_adjacent.validate(msg, as_of, symbols, known, active)
        except propose.InvalidProposal as exc:
            err = str(exc)[:300]
            log.warning("adjacent proposal rejected: %s", err)
    raise RuntimeError(f"adjacent proposal failed validation twice: {err}")
