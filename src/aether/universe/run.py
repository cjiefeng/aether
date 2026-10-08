"""The monthly universe review run (spec §6.7). Proposals only: Aether never edits the
watchlist; the owner applies a proposal through a PR to `watchlist.yaml`.

    running row (short write)
      → discovery (SEC full-text search, QTUM holdings, current pure-plays)      network, no LLM
      → T1 business excerpts + criteria 1/3/4 (SEC + price provider)              network, no LLM
      → deep research per candidate + the listing sweep (web search)              LLM, run budget
      → evidence rows (short write; their ids are what the proposal cites)
      → proposal (no tools) → validator (retry once) → gates (code wins)          LLM, run budget
      → M14 adjacent track (`adjacent.py`; its own cap, failure stops only that track)
      → M14 slot rules, strong candidates, shortlist (`slots.py`)                  code only
      → M14 full re-evaluation, `kind = full` only (`full.py`; both caps)           LLM
      → candidates + done (one short write) → one Telegram message (+ one per strong candidate)

M14 (spec §6.7.2): a `remove` for any of the active names needs a code-found qualifying event
since the previous review (`X<n>` refs, cited by the proposal); adds beyond the free slots become
`watch`, and an exceptional one is notified separately as a strong candidate.

No write transaction is held across a network or LLM call. Every LLM call carries the run's
`RunBudget` (`UNIVERSE_REVIEW_BUDGET_USD`), outside the daily soft budget. Hitting the cap fails
the run and sends nothing; so does any other failure (the error is stored, the job fails, and
the 2nd-of-month run retries a failed monthly review once).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, insert, select, update

from aether.alerts.candidates import AlertCandidate
from aether.config import (
    OverlayParams,
    ProfileParams,
    Sources,
    StrategiesConfig,
    ThesisConfig,
    UniverseConfig,
    Watchlist,
)
from aether.db.engine import write_tx
from aether.db.models import universe_candidates, universe_reviews
from aether.db.types import to_iso
from aether.llm.client import BudgetExceeded, LlmClient, LlmError, RunBudget
from aether.portfolio import thesis
from aether.portfolio.job import canon
from aether.portfolio.thesis import Name
from aether.providers.edgar import EdgarClient
from aether.providers.prices import PriceProvider, ProviderError
from aether.review.pack import fit
from aether.runs import JobResult
from aether.universe import propose
from aether.universe.adjacent import evidence_counts, run_adjacent_track
from aether.universe.business import fetch_business
from aether.universe.common import Assessed, assess_criteria, write_evidence
from aether.universe.discover import (
    Candidate,
    ExchangeMap,
    discover,
    latest_qtum,
    parse_exchange_map,
    parse_fts_hits,
)
from aether.universe.full import PoolEntry, propose_set
from aether.universe.full import assess as assess_full
from aether.universe.full import build_context as full_context
from aether.universe.full import cooldown_ok as full_cooldown_ok
from aether.universe.full import prompt_version as full_prompt_version
from aether.universe.gates import Facts, Gated, c3_streak, gate, is_change
from aether.universe.research import WebEvidence, dossier, sweep
from aether.universe.slots import (
    EvidenceCount,
    Trigger,
    apply_slots,
    qualifying_triggers,
    shortlist,
    strong_candidate,
)
from aether.universe.triggers import structural_triggers

log = logging.getLogger(__name__)

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
    # M14: the adjacent track's own cap; thesis thresholds; profiles for the full re-evaluation.
    adjacent_budget_usd: Decimal = Decimal("10.00")
    thesis: ThesisConfig = field(default_factory=ThesisConfig)
    strategies: StrategiesConfig | None = None


# --------------------------------------------------------------------------- DB helpers


def month_done(engine: Engine, month: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(universe_reviews.c.id).where(
                    universe_reviews.c.month == month,
                    universe_reviews.c.status == "done",
                    universe_reviews.c.kind != "full",
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
    """Previous done monthly/manual reviews per symbol, newest first: {month, as_of, action,
    criteria}. Full re-evaluations (M14) are excluded: they'd double-count a month."""
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
                universe_reviews.c.as_of,
            )
            .join(universe_reviews, universe_reviews.c.id == universe_candidates.c.review_id)
            .where(
                universe_reviews.c.status == "done",
                universe_reviews.c.kind != "full",
                universe_reviews.c.id < before_id,
                universe_candidates.c.symbol.in_(list(symbols)),
            )
            .order_by(universe_reviews.c.id.desc())
        ).all()
    for r in rows:
        out[r.symbol].append(
            {
                "month": r.month,
                "as_of": r.as_of,
                "action": r.action,
                "criteria": json.loads(r.criteria),
            }
        )
    return out


# --------------------------------------------------------------------------- steps


def previous_review(engine: Engine, before_id: int) -> str | None:
    """The previous done monthly/manual review's date (the §6.7.2 trigger window start)."""
    with engine.connect() as conn:
        d: str | None = conn.execute(
            select(universe_reviews.c.as_of)
            .where(
                universe_reviews.c.status == "done",
                universe_reviews.c.kind != "full",
                universe_reviews.c.id < before_id,
            )
            .order_by(universe_reviews.c.id.desc())
            .limit(1)
        ).scalar()
    return d


def last_full_review(engine: Engine) -> str | None:
    """When the latest non-failed full re-evaluation was requested (the 7-day cooldown)."""
    with engine.connect() as conn:
        d: str | None = conn.execute(
            select(universe_reviews.c.created_at)
            .where(universe_reviews.c.kind == "full", universe_reviews.c.status != "failed")
            .order_by(universe_reviews.c.id.desc())
            .limit(1)
        ).scalar()
    return d


def _discover(
    deps: UniverseDeps, engine: Engine, today: date
) -> tuple[Any, str | None, ExchangeMap, dict[str, float]]:
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
    # M14: adjacent-industry seeds and names belong to the adjacent track, and hyperscalers are
    # excluded from every review (spec §6.7.1, §6.7.3): never pure-play candidates.
    adj = cfg.adjacent
    if adj is not None:
        seeds = {s for sec in adj.sectors.values() for s in sec.seeds}
        kept: list[Candidate] = []
        for c in found.candidates:
            if not c.on_watchlist and c.symbol in adj.excluded_symbols:
                found.screened.append(
                    {
                        "symbol": c.symbol,
                        "name": c.name,
                        "reason": "excluded: hyperscaler / cloud platform",
                    }
                )
            elif not c.on_watchlist and c.symbol in seeds:
                found.screened.append(
                    {
                        "symbol": c.symbol,
                        "name": c.name,
                        "reason": "adjacent-industry seed: reviewed in the adjacent track",
                    }
                )
            else:
                kept.append(c)
        found.candidates = kept
    return found, snap, xmap, qtum


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


def _propose(
    deps: UniverseDeps,
    budget: RunBudget,
    as_of: str,
    context: str,
    symbols: list[str],
    known: set[str],
    active: set[str] | None = None,
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
            return propose.validate(msg, as_of, symbols, known, active)
        except propose.InvalidProposal as exc:
            err = str(exc)[:300]
            log.warning("universe proposal rejected: %s (%s)", err, propose.prompt_version())
    raise RuntimeError(f"proposal failed validation twice: {err}")


# --------------------------------------------------------------------------- M14 helpers


def sleeve_names(watchlist: Watchlist) -> list[Name]:
    return [Name(t.symbol, t.type, t.modality, t.sector) for t in watchlist.sleeve()]


def current_lines(
    engine: Engine,
    names: Sequence[Name],
    today: date,
    cfg: ThesisConfig,
    triggers: Mapping[str, Sequence[Trigger]],
) -> tuple[list[str], dict[str, Any]]:
    """Computed lines about the active names for the proposal (tags, red-flag counts, X refs),
    and the per-name thesis checks. Computed metrics only: no thesis text."""
    with engine.connect() as conn:
        checks = thesis.name_checks(conn, names, today, cfg)
    lines = []
    for n in names:
        c = checks[n.symbol]
        flags = [f["label"].lower() for f in c["red_flags"] if f["status"] == "flag"]
        kind = "pure-play" if n.type == "pure_play" else "adjacent industry"
        refs = [t.ref for t in triggers.get(n.symbol, [])]
        lines.append(
            f"{n.symbol} ({kind}, {n.category.replace('_', ' ')}): "
            f"{len(flags)} computed red flag(s){': ' + ', '.join(flags) if flags else ''}"
            + (f"; qualifying events {', '.join(refs)}" if refs else "")
        )
    return lines, checks


def _evidence_note(items: Sequence[propose.EvidenceItem], cited: Sequence[str]) -> str:
    out = [f"{e.trust_tier} {e.domain} {e.published or 'undated'}" for e in items if e.ref in cited]
    return "; ".join(out[:4]) or "none cited"


def strong_text(row: Mapping[str, Any], items: Sequence[propose.EvidenceItem], why: str) -> str:
    c = row["criteria_obj"]
    mcap = row.get("market_cap_micros")
    wc = c.get("weakest_current") or {}
    cited = [i for r in row["reasons"] for i in r["evidence_ids"]]
    lines = [
        f"Aether · strong candidate (#10) · {row['symbol']} ({row['name']})",
        f"Track: {'pure-play' if row['track'] == 'pure_play' else 'adjacent'}"
        + (f" · {row['sector'].replace('_', ' ')}" if row.get("sector") else ""),
        f"What it does: {_clip(row['description'] or '', 300)}",
        f"Why it's strong ({why}): " + _clip(row["reasons"][0]["text"], 300),
        f"Evidence: {c.get('evidence_independent', 0)} independent source(s) in 12 months, "
        f"{c.get('evidence_t1', 0)} T1. Cited: {_evidence_note(items, cited)}.",
        "Market cap: "
        + (
            f"${int(mcap):,} ({row.get('mcap_bucket')}, close {c.get('close_date')}, "
            f"{c.get('shares_source') or 'unknown'} shares)"
            if mcap is not None
            else "unknown"
        ),
        "QTUM overlap: "
        + (
            f"{row['overlap']['qtum_weight_pct']:.2f}% of the fund"
            if row["overlap"].get("qtum_weight_pct") is not None
            else "not held"
        ),
        "Compares least favourably with: "
        + (f"{wc['symbol']}: {_clip(wc.get('text') or '', 240)}" if wc else "not given"),
        "Adding needs a slot: remove a name or raise the cap. Review on /universe.",
        "Not financial advice.",
    ]
    return fit(lines, where=WHERE)


# --------------------------------------------------------------------------- telegram


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _why(r: Mapping[str, Any]) -> str:
    return r["gate_note"] or (r["reasons"][0]["text"] if r["reasons"] else "")


def telegram_text(
    as_of: str, rows: Sequence[dict[str, Any]], payload: dict[str, Any], cost: Decimal
) -> str:
    """Plain text, ≤ 4096 chars; anything past the limit says how much more is on /universe."""
    full = payload.get("full")
    title = "Aether full re-evaluation" if full else "Aether universe review"
    lines = [
        f"{title} · {as_of}",
        "Proposals only: apply one by editing watchlist.yaml in a PR.",
        "",
    ]
    if full:
        lines += [*payload.get("full_lines", []), ""]
    pure = [r for r in rows if r.get("track", "pure_play") == "pure_play"]
    adj = [r for r in rows if r.get("track") == "adjacent"]
    adds = [r for r in pure if r["action"] == "add"]
    removes = [r for r in pure if r["action"] == "remove"]
    watch = [r for r in pure if r["action"] == "watch"]
    if adj or (payload.get("adjacent") or {}).get("status", "skipped") != "skipped":
        lines.append("Pure-plays:")
    if not adds and not removes:
        lines.append("No changes proposed.")
    for title_, group in (("Add:", adds), ("Remove:", removes)):
        if group:
            lines.append(title_)
            for r in group:
                lines.append(f"- {r['symbol']} ({r['name']}): {_clip(_why(r), DESC_TELEGRAM)}")
    if watch:
        lines += ["", "Watch:"]
        for r in watch:
            lines.append(f"- {r['symbol']}: {_clip(_why(r), DESC_TELEGRAM)}")
    anns = payload.get("announcements") or []
    if anns:
        lines += ["", f"Announced listings (web, unverified): {len(anns)}"]
    a = payload.get("adjacent")
    if a and a.get("status") != "skipped":
        lines += ["", "Adjacent industries:"]
        if a.get("status") != "done":
            lines.append(f"Track {a.get('status')}: {_clip(a.get('error') or '', 200)}")
        else:
            changes = [r for r in adj if r["action"] in ("add", "remove")]
            if not changes:
                lines.append("No changes proposed.")
            for r in changes:
                lines.append(
                    f"- {r['action'].capitalize()} {r['symbol']} "
                    f"({(r.get('sector') or '').replace('_', ' ')}, {r.get('mcap_bucket') or '?'} "
                    f"cap, exposure {r.get('exposure') or '?'}): {_clip(_why(r), DESC_TELEGRAM)}"
                )
            short = payload.get("shortlist") or []
            if short:
                lines.append("Shortlist: " + ", ".join(short))
            info = a.get("info") or []
            if info:
                n_priv = sum(i.get("status") == "not investable" for i in info)
                n_out = sum(i.get("status") == "outside mandate" for i in info)
                lines.append(f"Not investable: {n_priv}; outside mandate: {n_out}.")
    sl = payload.get("slots")
    if sl:
        lines += [
            "",
            f"Names: {sl['active']} of {sl['cap']} used; {sl['free']} slot(s) free after "
            f"proposed removals.",
        ]
        if payload.get("strong"):
            lines.append("Strong candidates (separate message): " + ", ".join(payload["strong"]))
    c = payload["counts"]
    lines += [
        "",
        f"Reviewed {c.get('reviewed', 0)} companies ({c.get('screened', 0)} screened out). "
        f"Cost ${cost:.2f}.",
        f"Sources and criteria: {WHERE}. Not financial advice.",
    ]
    return fit(lines, where=WHERE)


# --------------------------------------------------------------------------- full review


def _profile(engine: Engine, deps: UniverseDeps) -> tuple[str, ProfileParams]:
    from aether.portfolio.holdings import load_settings

    profile = load_settings(engine).selected_profile
    if deps.strategies is not None:
        return profile, deps.strategies.profiles[profile]
    return profile, FALLBACK_PROFILE


FALLBACK_PROFILE = ProfileParams(
    qtum_weight=0.45,
    max_per_name=0.20,
    vol_limit_x=None,
    max_dd_limit_pp=None,
    rank_metric="sortino_high",
    min_per_name=0.03,
)


def _pool(
    rows: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Sequence[propose.EvidenceItem]],
    names: Sequence[Name],
    checks: Mapping[str, Any],
    triggers: Mapping[str, Sequence[Trigger]],
) -> list[PoolEntry]:
    current = {n.symbol: n for n in names}
    out: list[PoolEntry] = []
    for r in rows:
        c = r["criteria_obj"]
        sym = r["symbol"]
        is_current = sym in current
        if not is_current and not c.get("add_qualified"):
            continue
        computed = []
        mcap = c.get("market_cap")
        bucket = r.get("mcap_bucket")
        computed.append(
            f"market cap {'$' + format(int(mcap), ',') if mcap else 'unknown'}"
            + (f" ({bucket})" if bucket else "")
        )
        if r.get("exposure"):
            computed.append(f"quantum exposure {r['exposure']} (code-bounded)")
        if is_current and sym in checks:
            flags = [f["label"].lower() for f in checks[sym]["red_flags"] if f["status"] == "flag"]
            computed.append(
                f"{len(flags)} red flag(s)" + (": " + ", ".join(flags) if flags else "")
            )
        computed.append(
            f"fills an uncovered modality or industry: {'yes' if c.get('fills_gap') else 'no'}"
        )
        q = r["overlap"].get("qtum_weight_pct")
        computed.append("QTUM overlap " + (f"{q:.2f}% of the fund" if q is not None else "none"))
        category = current[sym].category if is_current else r.get("sector")
        out.append(
            PoolEntry(
                symbol=sym,
                track=r["track"],
                current=is_current,
                name=r["name"],
                category=category,
                computed=computed,
                triggers=[t.line() for t in triggers.get(sym, [])],
                evidence=list(evidence.get(sym, [])),
            )
        )
    return out


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
    if kind == "full" and not full_cooldown_ok(
        last_full_review(engine), today, deps.cfg.full_review_cooldown_days
    ):
        days = deps.cfg.full_review_cooldown_days
        raise FullReviewCooldown(f"a full re-evaluation already ran in the last {days} days")
    rid = _claim(engine, deps, today, kind, now)
    budget = RunBudget(deps.budget_usd)
    adj_budget = RunBudget(deps.adjacent_budget_usd)
    full_budget: RunBudget | None = None
    payload: dict[str, Any] = {
        "as_of": today.isoformat(),
        "budget": str(deps.budget_usd),
        "adjacent_budget": str(deps.adjacent_budget_usd),
        "notes": [],
    }
    strong_alerts: list[AlertCandidate] = []

    def spent() -> Decimal:
        return budget.spent + adj_budget.spent + (full_budget.spent if full_budget else 0)

    try:
        found, snap, xmap, qtum = _discover(deps, engine, today)
        payload["qtum_snapshot"] = snap
        kept = _business(deps, found, today)
        assess_criteria(deps, kept, today)
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

        per, sweep_items = write_evidence(engine, rid, chosen, swept)
        symbols = [a.cand.symbol for a in chosen]
        hist = history(engine, symbols, rid)
        triggers = structural_triggers(
            engine, [a.cand.symbol for a in chosen if a.cand.on_watchlist], today, deps.overlay
        )

        # M14: the active names, their §6.7.2 qualifying events and computed thesis checks.
        names = sleeve_names(deps.watchlist)
        active = {n.symbol for n in names}
        prev = previous_review(engine, rid)
        since = date.fromisoformat(prev) if prev else today - timedelta(days=31)
        qual = qualifying_triggers(
            engine, sorted(active), since, today, deps.overlay, deps.cfg.removal
        )
        trigger_refs = {t.ref for ts in qual.values() for t in ts}
        cur_lines, checks = current_lines(engine, names, today, deps.thesis, qual)
        payload["triggers"] = {s: [t.line() for t in ts] for s, ts in qual.items() if ts}
        payload["triggers_since"] = since.isoformat()

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
                    qualifying=[t.line() for t in qual.get(a.cand.symbol, [])],
                )
            )
        context, known = propose.build_context(
            today.isoformat(), ctxs, sweep_items, trigger_refs, cur_lines
        )
        proposal = _propose(deps, budget, today.isoformat(), context, symbols, known, active)

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
                qualifying={t.ref: t.text for t in qual.get(sym, [])},
            )
            g: Gated = gate(facts, entry["action"], cited)
            mcap = Decimal(crit.detail["market_cap"]) if crit.detail.get("market_cap") else None
            t1, t2, _refs, latest = evidence_counts(per[sym], today, 365)
            wc = entry.get("weakest_current") or {}
            criteria_obj = {
                **crit.to_json(),
                "tracked": a.cand.on_watchlist,
                "c2_excerpt": excerpt_ref,
                "c2_note": a.excerpt_note,
                "announced": a.cand.announced,
                "sources": sorted(a.cand.sources),
                "structural": list(facts.structural),
                "c3_streak": facts.c3_streak,
                "qualifying": dict(facts.qualifying),
                # M14: thesis gap, evidence counts, strong-candidate inputs.
                "fills_gap": False if a.cand.on_watchlist else None,
                "evidence_t1": t1,
                "evidence_t2": t2,
                "evidence_independent": (1 if t1 else 0) + t2,
                "evidence_latest": latest,
                "weakest_current": wc if wc.get("symbol") else None,
            }
            rows.append(
                {
                    "symbol": sym,
                    "track": "pure_play",
                    "action": g.action,
                    "proposed_action": g.proposed,
                    "cik": a.cand.cik,
                    "name": entry["name"],
                    "overlap": {"qtum_weight_pct": a.cand.qtum_weight, "qtum_snapshot": snap},
                    "criteria_obj": criteria_obj,
                    "description": entry["description"],
                    "reasons": entry["reasons"],
                    "evidence_ids": [
                        int(i[1:])
                        for i in dict.fromkeys(cited + ([excerpt_ref] if excerpt_ref else []))
                        if i.startswith("U")
                    ],
                    "gate_note": g.note,
                    "sector": None,
                    # A pure-play's T1 excerpt shows quantum is its principal business.
                    "exposure": "high" if g.action == "add" else None,
                    "market_cap_micros": mcap,
                    "mcap_bucket": deps.cfg.mcap_buckets.bucket(mcap),
                }
            )
        found.counts["reviewed"] = len(chosen)
        found.counts["screened"] = len(found.screened)

        # M14: the adjacent-industry track (its own cap; a failure stops only this track).
        def hist_for(syms: Sequence[str]) -> Mapping[str, list[dict[str, Any]]]:
            return history(engine, syms, rid)

        adj = run_adjacent_track(
            engine,
            deps,
            rid,
            today=today,
            now=now,
            xmap=xmap,
            qtum=qtum,
            snap=snap,
            triggers=qual,
            current_lines=cur_lines,
            history=hist_for,
            fills_gap=lambda cat: thesis.fills_gap(cat, names),
            budget=adj_budget,
        )
        evidence: dict[str, list[propose.EvidenceItem]] = {**per, **adj.evidence}
        all_rows = rows + adj.rows

        # M14 slot rules, strong candidates and the shortlist (code only).
        priorities: dict[str, int] = (
            {str(k): v.priority for k, v in deps.cfg.adjacent.sectors.items()}
            if deps.cfg.adjacent
            else {}
        )
        slots = apply_slots(
            all_rows, active=len(active), cap=deps.cfg.max_names_ex_qtum, priorities=priorities
        )
        strong: list[str] = []
        for r in all_rows:
            c = r["criteria_obj"]
            if not c.get("slot_blocked"):
                continue
            ev = EvidenceCount(
                c.get("evidence_independent", 0), c.get("evidence_t1", 0), c.get("evidence_latest")
            )
            h = history(engine, [r["symbol"]], rid)[r["symbol"]]
            ok, why = strong_candidate(r, ev, deps.cfg.strong_candidate, h)
            c["strong"] = ok
            c["strong_note"] = why
            if ok:
                strong.append(r["symbol"])
                strong_alerts.append(
                    AlertCandidate(
                        kind="universe_strong_candidate",
                        dedupe_key=f"universe_strong_candidate:{rid}:{r['symbol']}",
                        text=strong_text(r, evidence.get(r["symbol"], []), why),
                        payload={"review_id": rid, "symbol": r["symbol"]},
                    )
                )
        payload.update(
            {
                "counts": found.counts,
                "screened": found.screened,
                "announcements": proposal.announcements,
                "changes": sum(is_change(r["action"]) for r in all_rows),
                "prompt_version": propose.prompt_version(),
                "adjacent": adj.payload(),
                "slots": slots,
                "strong": strong,
                "shortlist": shortlist(
                    all_rows,
                    priorities,
                    deps.cfg.adjacent.shortlist_size if deps.cfg.adjacent else 5,
                ),
                "gaps": thesis.gaps(names),
                "names_used": len(active),
            }
        )
        if adj.status == "failed":
            payload["notes"].append(f"adjacent track failed: {adj.error}"[:300])

        if kind == "full":
            left = deps.budget_usd + deps.adjacent_budget_usd - budget.spent - adj_budget.spent
            full_budget = RunBudget(max(Decimal(0), left))
            pool = _pool(all_rows, evidence, names, checks, qual)
            fctx, fknown = full_context(
                today.isoformat(), deps.cfg.max_names_ex_qtum, pool, trigger_refs
            )
            excluded = set(deps.cfg.adjacent.excluded_symbols) if deps.cfg.adjacent else set()
            raw = propose_set(
                deps.llm,
                deps.model,
                full_budget,
                as_of=today.isoformat(),
                context=fctx,
                pool=pool,
                known=fknown,
                cap=deps.cfg.max_names_ex_qtum,
                excluded=excluded,
                max_tokens=deps.cfg.proposal_max_tokens,
                effort=deps.cfg.proposal_effort,
                attempts=deps.cfg.proposal_max_attempts,
            )
            profile, pp = _profile(engine, deps)
            res = assess_full(raw, pool, names, pp, profile, deps.thesis)
            payload["full"] = {
                **res.payload,
                "cap": deps.cfg.max_names_ex_qtum,
                "prompt_version": full_prompt_version(),
            }
            payload["full_lines"] = res.text_lines
            strong_alerts = []  # the full re-evaluation sends one summary only (§6.7.3)
        text = telegram_text(today.isoformat(), all_rows, payload, spent())
    except Exception as exc:
        reason = f"budget cap reached: {exc}" if isinstance(exc, BudgetExceeded) else repr(exc)
        _fail(engine, rid, reason, spent(), payload)
        if isinstance(exc, BudgetExceeded):
            raise RuntimeError(f"universe review stopped at the run cap: {exc}"[:500]) from None
        raise

    with write_tx(engine) as conn:
        if all_rows:
            conn.execute(
                insert(universe_candidates),
                [
                    {
                        **{k: v for k, v in r.items() if k != "criteria_obj"},
                        "review_id": rid,
                        "overlap": canon(r["overlap"]),
                        "criteria": canon(r["criteria_obj"]),
                        "reasons": canon(r["reasons"]),
                        "evidence_ids": canon(r["evidence_ids"]),
                    }
                    for r in all_rows
                ],
            )
        conn.execute(
            update(universe_reviews)
            .where(universe_reviews.c.id == rid)
            .values(
                status="done",
                payload=canon(payload),
                prompt_version=propose.prompt_version(),
                cost_micros=spent(),
                telegram_text=text,
                finished_at=to_iso(datetime.now(UTC)),
            )
        )
    key = f"universe_review:{month}" if kind == "monthly" else f"universe_review:{kind}:{rid}"
    deps.notify(
        [
            AlertCandidate(
                kind="universe_review", dedupe_key=key, text=text, payload={"review_id": rid}
            ),
            *strong_alerts,
        ]
    )
    return JobResult(
        rows_written=len(all_rows),
        provider=deps.model,
        warning="; ".join(payload["notes"]) or None,
    )


class FullReviewCooldown(RuntimeError):
    pass
