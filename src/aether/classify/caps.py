"""Trust-tier materiality caps (spec S1, §5.2 step 3), enforced in code after the classifier.

- any T1 source (SEC EDGAR, the company's own domain) → no cap (5);
- two or more independent T2 sources (distinct registrable domains, syndicated copies excluded)
  → no cap (5);
- a single independent T2 source → at most 3;
- T3 sources only → at most 2.

A syndicated T1 copy still counts as T1: the cap is about who published, not independence.
`materiality_raw` keeps the classifier's value; `materiality` is always min(raw, cap), so a later
merge of a better source lifts the stored value back towards raw.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, select, update

from aether.db.models import event_classifications, event_sources
from aether.domains import registrable_domain

UNCAPPED = 5
SINGLE_T2_CAP = 3
T3_ONLY_CAP = 2


def cap_for(sources: Iterable[tuple[str, str, bool]]) -> int:
    """`sources` = (domain, trust_tier, syndicated) per event source."""
    t2_domains: set[str] = set()
    for domain, tier, syndicated in sources:
        if tier == "T1":
            return UNCAPPED
        if tier == "T2" and not syndicated:
            t2_domains.add(registrable_domain(domain))
    if len(t2_domains) >= 2:
        return UNCAPPED
    if len(t2_domains) == 1:
        return SINGLE_T2_CAP
    return T3_ONLY_CAP


def event_cap(conn: Connection, event_id: int) -> int:
    rows = conn.execute(
        select(
            event_sources.c.domain, event_sources.c.trust_tier, event_sources.c.syndicated
        ).where(event_sources.c.event_id == event_id)
    ).all()
    return cap_for((d, t, bool(s)) for d, t, s in rows)


def apply_caps(conn: Connection, event_id: int) -> None:
    """Re-derive `materiality` from `materiality_raw` and the event's current sources (no-op
    for an unclassified event). Runs inside the caller's write transaction."""
    raw = conn.execute(
        select(event_classifications.c.materiality_raw).where(
            event_classifications.c.event_id == event_id
        )
    ).scalar()
    if raw is None:
        return
    conn.execute(
        update(event_classifications)
        .where(event_classifications.c.event_id == event_id)
        .values(materiality=min(int(raw), event_cap(conn, event_id)))
    )
