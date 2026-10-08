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
- the model's `remove` stands only when it cites the T1 business excerpt (criterion 2 fails);
  otherwise → `watch` ("concerns, no qualifying trigger");
- `add` / `skip` → `keep` (already tracked).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


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
            if f.excerpt_ref is not None and f.excerpt_ref in cited:
                return Gated("remove", proposed, None)
            return Gated(
                "watch",
                proposed,
                "concerns, no qualifying trigger: a removal needs an acquisition/delisting, "
                "criterion 3 failing in consecutive reviews, or a cited T1 business excerpt",
            )
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
