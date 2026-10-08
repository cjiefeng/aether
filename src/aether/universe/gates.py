"""Deterministic gates on the model's proposal (spec §6.7): code decides, the model can't
override. Pure functions.

New candidates (not tracked):
- `add` survives only if criteria 1, 3 and 4 pass **and** the T1 business excerpt exists and is
  cited by the entry. Otherwise it becomes `watch`, with the blocking reasons in `gate_note`
  (criterion 4 alone, or an announced listing, is the spec's "watch, not add" case).
- `remove` / `keep` make no sense for an untracked company → `skip`.

Current pure-plays (tracked):
- a structural trigger found in code (acquired/delisted, a listing-deficiency notice, an 8-K
  reporting both Item 2.01 and 5.01) → `remove`, whatever the model said;
- criterion 3 failing in this review and the previous N-1 done reviews → `remove`;
- the model's `remove` stands only when it cites the T1 business excerpt (criterion 2 fails) or,
  from M14, a code-found qualifying event (`X<n>`, spec §6.7.2: RISK >= 4 with T1 or 2 x T2, an
  overlay hard rule, an accepted AVOID); otherwise → `watch` ("concerns, no qualifying event");
- `add` / `skip` → `keep` (already tracked).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

NO_QUALIFYING_EVENT = (
    "concerns, no qualifying event: a removal needs an acquisition/delisting, a structural "
    "trigger, or a cited qualifying event (RISK >= 4 with a T1 or 2 independent T2 sources, an "
    "overlay hard rule, or an accepted AVOID)"
)


@dataclass(frozen=True)
class Facts:
    """What code knows about one candidate."""

    tracked: bool
    c1: bool
    c3: bool
    c4: bool
    announced: bool
    excerpt_ref: str | None  # "U12" when a T1 business excerpt exists
    c3_fails: Sequence[str]  # criterion-3 failure reasons (this review)
    c4_sessions: int
    min_sessions: int
    structural: Sequence[str]  # code-found removal triggers (plain text)
    c3_streak: bool  # criterion 3 failed in this and the previous N-1 reviews
    qualifying: Mapping[str, str] = field(default_factory=dict)  # M14: X ref -> trigger text


@dataclass(frozen=True)
class Gated:
    action: str
    proposed: str | None
    note: str | None


def gate(f: Facts, proposed: str | None, cited: Sequence[str]) -> Gated:
    if f.tracked:
        if f.structural:
            return Gated("remove", proposed, "removal trigger (code): " + "; ".join(f.structural))
        if f.c3_streak:
            return Gated(
                "remove",
                proposed,
                "criterion 3 failed in consecutive reviews (code): " + "; ".join(f.c3_fails),
            )
        if proposed == "remove":
            hit = [r for r in cited if r in f.qualifying]
            if hit:
                return Gated(
                    "remove",
                    proposed,
                    "qualifying event (code): " + "; ".join(f.qualifying[r] for r in hit),
                )
            if f.excerpt_ref is not None and f.excerpt_ref in cited:
                return Gated("remove", proposed, None)
            return Gated("watch", proposed, NO_QUALIFYING_EVENT)
        if proposed == "watch":
            return Gated("watch", proposed, None)
        note = "already tracked" if proposed in ("add", "skip") else None
        return Gated("keep", proposed, note)

    if proposed in ("remove", "keep"):
        return Gated("skip", proposed, "not tracked: nothing to keep or remove")
    if proposed != "add":
        return Gated(proposed or "skip", proposed, None)
    if not f.c1 and f.announced:
        # Not trading yet: the price-based criteria can't apply.
        return Gated(
            "watch", proposed, "add blocked: listing not closed (IPO registration on EDGAR)"
        )
    blockers: list[str] = []
    if not f.c1:
        blockers.append("not listed on NYSE/Nasdaq")
    if not f.c3:
        blockers += list(f.c3_fails) or ["criterion 3 not met"]
    if not f.c4:
        blockers.append(f"history {f.c4_sessions} < {f.min_sessions} sessions")
    if f.excerpt_ref is None:
        blockers.append("no T1 business excerpt")
    elif f.excerpt_ref not in cited:
        blockers.append("the T1 business excerpt isn't cited")
    if blockers:
        return Gated("watch", proposed, "add blocked: " + "; ".join(blockers))
    return Gated("add", proposed, None)


def is_change(action: str) -> bool:
    return action in ("add", "remove")


def c3_streak(current_fail: bool, history: Sequence[Mapping[str, Any]], n: int) -> bool:
    """`history`: previous done reviews' criteria for this symbol, newest first. Only measured
    failures count: a review where criterion 3 couldn't be computed breaks the streak."""
    if not current_fail:
        return False
    prev = [h for h in history if "c3" in h][: n - 1]
    return len(prev) == n - 1 and all(not h["c3"] and not h.get("c3_unknown") for h in prev)


# --------------------------------------------------------------------------- M14 adjacent track


@dataclass(frozen=True)
class AdjFacts:
    """What code knows about one adjacent-track candidate (spec §6.7.1)."""

    tracked: bool
    c1: bool
    c3: bool
    c4: bool
    c3_fails: Sequence[str]
    c4_sessions: int
    min_sessions: int
    structural: Sequence[str]  # acquired/delisted, no quantum evidence, excluded category
    qualifying: Mapping[str, str]  # X ref -> §6.7.2 trigger text
    t1_items: int  # qualifying T1 items (quantum-related, last 12 months)
    t2_domains: int  # independent T2 domains (quantum-related, last 12 months)
    min_t2: int
    t1_quantum_refs: frozenset[str]  # refs of T1 items mentioning quantum (exposure cap)
    c3_streak: bool = False


@dataclass(frozen=True)
class AdjGated:
    action: str
    proposed: str | None
    note: str | None
    exposure: str | None
    exposure_note: str | None


def cap_exposure(
    proposed: str | None, cited: Sequence[str], t1_refs: frozenset[str]
) -> tuple[str | None, str | None]:
    """`high` needs a cited T1 item showing quantum-related products; otherwise `med`."""
    if proposed == "high" and not (set(cited) & t1_refs):
        return (
            "med",
            "exposure capped at med: no cited T1 excerpt shows a quantum-related product line",
        )
    return proposed, None


def gate_adjacent(f: AdjFacts, entry: Mapping[str, Any], cited: Sequence[str]) -> AdjGated:
    proposed = entry.get("action")
    exposure, exp_note = cap_exposure(
        entry.get("exposure"), entry.get("exposure_evidence_ids") or [], f.t1_quantum_refs
    )

    def out(action: str, note: str | None) -> AdjGated:
        return AdjGated(action, proposed, note, exposure, exp_note)

    if f.tracked:
        if f.structural:
            return out("remove", "removal trigger (code): " + "; ".join(f.structural))
        if f.c3_streak:
            return out(
                "remove",
                "size/liquidity floors failed in consecutive reviews (code): "
                + "; ".join(f.c3_fails),
            )
        if proposed == "remove":
            hit = [r for r in cited if r in f.qualifying]
            if hit:
                return out(
                    "remove",
                    "qualifying event (code): " + "; ".join(f.qualifying[r] for r in hit),
                )
            return out("watch", NO_QUALIFYING_EVENT)
        if proposed == "watch":
            return out("watch", None)
        return out("keep", "already tracked" if proposed in ("add", "skip") else None)

    if proposed in ("remove", "keep"):
        return out("skip", "not tracked: nothing to keep or remove")
    if proposed != "add":
        return out(proposed or "skip", None)
    blockers: list[str] = []
    if not f.c1:
        blockers.append("not listed on NYSE/Nasdaq")
    if f.t1_items < 1 and f.t2_domains < f.min_t2:
        blockers.append(
            f"quantum-related evidence: {f.t1_items} T1 item(s) and {f.t2_domains} independent "
            f"T2 source(s) in 12 months (needs 1 T1 or {f.min_t2} T2)"
        )
    if not f.c3:
        blockers += list(f.c3_fails) or ["size/liquidity floors not met"]
    if not f.c4:
        blockers.append(f"history {f.c4_sessions} < {f.min_sessions} sessions")
    if blockers:
        return out("watch", "add blocked: " + "; ".join(blockers))
    return out("add", None)
