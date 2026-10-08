"""Slot rules (spec §6.7.2, M14; both review tracks). Deterministic: the model proposes, code
decides.

**Removals: any of the 9, on serious bad news.** Besides each track's structural triggers, a
`remove` for any active name stands only when it cites a *qualifying trigger* found in code since
the previous review:
- a non-quarantined RISK event at or above `removal.min_materiality` (post-cap), backed by a T1
  source or `removal.min_independent_t2` independent T2 domains (syndicated copies don't count);
- an overlay hard rule (§6.6.1 layer 1) currently zeroing the name;
- an AVOID stance accepted by hysteresis (not `held`).
Each trigger gets a citable ref `X<n>`; without one, a `remove` becomes `watch` ("concerns, no
qualifying event").

**Adds when the slots are full: notify, don't propose.** Free slots = cap - active names + this
review's removals. Adds beyond the free slots become `watch`. A candidate that passed every add
criterion is a **strong candidate** (#10) only when code confirms: exposure `high` (code-validated),
>= `min_independent_sources` independent qualifying (T1/T2) evidence items in the last 12 months
with >= `min_t1_sources` of them T1, floors met, not excluded, and not escalated in the last
`cooldown_reviews` reviews unless there's newer qualifying evidence.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, select

from aether.config import OverlayParams, RemovalRule, StrongCandidateRule
from aether.db.models import (
    conclusions,
    event_classifications,
    event_sources,
    event_tickers,
    events,
)
from aether.portfolio.overlay import layer1_findings

DEFAULT_LOOKBACK_DAYS = 31
EXPOSURE_RANK = {"high": 0, "med": 1, "low": 2, None: 3}


@dataclass(frozen=True)
class Trigger:
    ref: str  # X1, X2, … (citable in the proposal)
    symbol: str
    kind: str  # risk_event | overlay | avoid
    text: str

    def line(self) -> str:
        return f"{self.ref}: {self.text}"


def qualifying_triggers(
    engine: Engine,
    symbols: Sequence[str],
    since: date,
    as_of: date,
    overlay: OverlayParams,
    rule: RemovalRule,
    start: int = 1,
) -> dict[str, list[Trigger]]:
    """Code-found §6.7.2 triggers per active symbol, since `since` (the previous review)."""
    out: dict[str, list[Trigger]] = {s: [] for s in symbols}
    if not symbols:
        return out
    raw: list[tuple[str, str, str]] = []
    since_iso = since.isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                events.c.id,
                events.c.title,
                events.c.published_at,
                events.c.trust_tier,
                events.c.source_domain,
                event_tickers.c.symbol,
                event_classifications.c.category,
                event_classifications.c.materiality,
            )
            .join(event_tickers, event_tickers.c.event_id == events.c.id)
            .join(event_classifications, event_classifications.c.event_id == events.c.id)
            .where(
                event_tickers.c.symbol.in_(list(symbols)),
                event_classifications.c["class"] == "RISK",
                event_classifications.c.materiality >= rule.min_materiality,
                events.c.quarantined == 0,
                events.c.injection_suspected == 0,
                events.c.published_at >= since_iso,
                events.c.published_at <= as_of.isoformat() + "T23:59:59Z",
            )
            .order_by(events.c.published_at, events.c.id)
        ).all()
        for r in rows:
            srcs = conn.execute(
                select(event_sources.c.domain, event_sources.c.trust_tier).where(
                    event_sources.c.event_id == r.id, event_sources.c.syndicated == 0
                )
            ).all()
            t1 = r.trust_tier == "T1" or any(t == "T1" for _d, t in srcs)
            t2 = {d for d, t in srcs if t == "T2"}
            if r.trust_tier == "T2":
                t2.add(r.source_domain)
            if not t1 and len(t2) < rule.min_independent_t2:
                continue
            backing = "T1 source" if t1 else f"{len(t2)} independent T2 sources"
            raw.append(
                (
                    r.symbol,
                    "risk_event",
                    f"RISK {r.category.replace('_', ' ')} (materiality {r.materiality}/5, "
                    f"{backing}), event #{r.id} published {r.published_at[:10]}",
                )
            )
        for f in layer1_findings(conn, symbols, as_of, overlay):
            if f.multiplier == 0:
                raw.append(
                    (
                        f.symbol,
                        "overlay",
                        f"overlay hard rule zeroing the name: {f.evidence()['label']} "
                        f"({f.form} {f.accession} filed {f.filed_at})",
                    )
                )
        for c in conn.execute(
            select(conclusions.c.id, conclusions.c.symbol, conclusions.c.as_of)
            .where(
                conclusions.c.kind == "ticker",
                conclusions.c.symbol.in_(list(symbols)),
                conclusions.c.stance == "AVOID",
                conclusions.c.held == 0,
                conclusions.c.as_of >= since_iso,
                conclusions.c.as_of <= as_of.isoformat(),
            )
            .order_by(conclusions.c.as_of, conclusions.c.id)
        ):
            raw.append(
                (c.symbol, "avoid", f"AVOID stance accepted (conclusion #{c.id}, {c.as_of})")
            )
    n = start
    for sym, kind, text in sorted(raw, key=lambda x: (x[0], x[1], x[2])):
        out[sym].append(Trigger(f"X{n}", sym, kind, text))
        n += 1
    return out


def previous_review_date(history_rows: Iterable[Mapping[str, Any]], as_of: date) -> date:
    """The previous done review's date, else `as_of` minus a month."""
    for h in history_rows:
        if h.get("as_of"):
            return date.fromisoformat(h["as_of"])
    return as_of - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def gate_removal(
    proposed: str | None, cited: Sequence[str], triggers: Sequence[Trigger]
) -> tuple[bool, str | None]:
    """(qualifies, note) for a `remove` with no structural trigger."""
    refs = {t.ref for t in triggers}
    hit = [r for r in cited if r in refs]
    if proposed == "remove" and hit:
        return True, "qualifying event (code): " + "; ".join(
            t.text for t in triggers if t.ref in hit
        )
    return False, None


# --------------------------------------------------------------------------- evidence counts


@dataclass(frozen=True)
class EvidenceCount:
    independent: int  # distinct domains among qualifying (T1/T2) items in the last 12 months
    t1: int
    latest: str | None  # newest qualifying item's date


def count_evidence(items: Iterable[Any], as_of: date, days: int = 365) -> EvidenceCount:
    """`items`: objects with trust_tier, domain, published (YYYY-MM-DD or None). Undated items
    count only when T1 (a filing, dated by SEC) is impossible to tell; they're skipped."""
    since = (as_of - timedelta(days=days)).isoformat()
    domains: set[str] = set()
    t1: set[str] = set()
    latest: str | None = None
    for e in items:
        if e.trust_tier not in ("T1", "T2") or not e.published or e.published < since:
            continue
        domains.add(e.domain)
        if e.trust_tier == "T1":
            t1.add(e.domain if e.kind != "business_excerpt" else f"sec.gov:{e.id}")
        latest = max(latest or "", e.published)
    return EvidenceCount(len(domains), len(t1), latest)


# --------------------------------------------------------------------------- slots


def _priority(r: Mapping[str, Any], priorities: Mapping[str, int]) -> int:
    # Pure-play adds rank with the top sector priority: they are the theme itself.
    return 1 if r["track"] == "pure_play" else priorities.get(r.get("sector") or "", 9)


def rank_key(r: Mapping[str, Any], priorities: Mapping[str, int]) -> tuple[Any, ...]:
    """Slot order: fills a gap first, then sector priority, exposure, evidence, symbol."""
    c = r.get("criteria_obj") or {}
    return (
        not c.get("fills_gap", False),
        _priority(r, priorities),
        EXPOSURE_RANK.get(r.get("exposure")),
        -int(c.get("evidence_independent", 0)),
        r["track"] != "pure_play",
        r["symbol"],
    )


def apply_slots(
    rows: list[dict[str, Any]],
    *,
    active: int,
    cap: int,
    priorities: Mapping[str, int],
) -> dict[str, Any]:
    """Turn adds beyond the free slots into `watch`, in rank order. Mutates `rows`; returns the
    slot summary. Every add-qualified row gets `criteria_obj['add_qualified'] = True` first."""
    removals = sum(r["action"] == "remove" for r in rows)
    free = max(0, cap - active + removals)
    adds = sorted((r for r in rows if r["action"] == "add"), key=lambda r: rank_key(r, priorities))
    for r in rows:
        if r["action"] == "add":
            r["criteria_obj"]["add_qualified"] = True
    kept = adds[:free]
    for r in adds[free:]:
        r["action"] = "watch"
        r["gate_note"] = (
            f"no free slot: {active} of {cap} names used"
            + (f", {removals} removal(s) proposed" if removals else "")
            + ". Adding needs a slot: remove a name or raise the cap."
        )
        r["criteria_obj"]["slot_blocked"] = True
    return {
        "cap": cap,
        "active": active,
        "removals": removals,
        "free": free,
        "adds": [r["symbol"] for r in kept],
        "blocked": [r["symbol"] for r in adds[free:]],
    }


def strong_candidate(
    row: Mapping[str, Any],
    ev: EvidenceCount,
    rule: StrongCandidateRule,
    history: Sequence[Mapping[str, Any]],
) -> tuple[bool, str]:
    """Whether a slot-blocked candidate is notified as a strong candidate, with the reason."""
    c = row.get("criteria_obj") or {}
    if not c.get("slot_blocked"):
        return False, "not slot-blocked"
    if row.get("exposure") != "high":
        return False, f"exposure {row.get('exposure') or 'unknown'}, not high"
    if ev.independent < rule.min_independent_sources:
        return False, (
            f"{ev.independent} independent qualifying source(s) in 12 months "
            f"(needs {rule.min_independent_sources})"
        )
    if ev.t1 < rule.min_t1_sources:
        return False, f"{ev.t1} T1 source(s) (needs {rule.min_t1_sources})"
    recent = [
        h for h in history[: rule.cooldown_reviews] if (h.get("criteria") or {}).get("strong")
    ]
    if recent:
        last = recent[0]
        if not ev.latest or ev.latest <= str(last.get("as_of") or ""):
            return False, f"already notified in {last.get('month')}; no newer qualifying evidence"
    return True, "meets every strong-candidate threshold"


def shortlist(
    rows: Sequence[Mapping[str, Any]], priorities: Mapping[str, int], n: int
) -> list[str]:
    """At most n untracked adjacent candidates (add or watch), sector priority first."""
    pool = [
        r
        for r in rows
        if r["track"] == "adjacent"
        and r["action"] in ("add", "watch")
        and not (r.get("criteria_obj") or {}).get("tracked")
    ]

    def key(r: Mapping[str, Any]) -> tuple[Any, ...]:
        k = rank_key(r, priorities)
        return (k[1], *k[2:4], k[0], r["symbol"])  # sector priority first

    return [r["symbol"] for r in sorted(pool, key=key)[:n]]


def loads(raw: str | None) -> Any:
    return json.loads(raw) if raw else None
