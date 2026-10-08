"""Read-only queries for the Universe page (spec §8.12, M12) and the review pack's M12 section.
Nothing here writes or calls an LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, select

from aether.db.models import universe_candidates, universe_evidence, universe_reviews
from aether.db.types import micros_to_decimal

ACTION_ORDER = {"add": 0, "remove": 1, "watch": 2, "keep": 3, "skip": 4}


@dataclass(frozen=True)
class ReviewRow:
    id: int
    as_of: str
    month: str
    kind: str
    status: str
    model: str
    cost: Decimal
    error: str | None
    created_at: str
    finished_at: str | None
    payload: dict[str, Any]


@dataclass
class CandidateRow:
    symbol: str
    action: str
    proposed_action: str | None
    name: str | None
    cik: str | None
    description: str | None
    criteria: dict[str, Any]
    overlap: dict[str, Any]
    reasons: list[dict[str, Any]]
    gate_note: str | None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def overridden(self) -> bool:
        return self.proposed_action is not None and self.proposed_action != self.action


def _review(r: Any) -> ReviewRow:
    cost = (
        r.cost_micros
        if isinstance(r.cost_micros, Decimal)
        else micros_to_decimal(int(r.cost_micros))
    )
    return ReviewRow(
        r.id,
        r.as_of,
        r.month,
        r.kind,
        r.status,
        r.model,
        cost,
        r.error,
        r.created_at,
        r.finished_at,
        json.loads(r.payload or "{}"),
    )


def reviews(engine: Engine, limit: int = 24) -> list[ReviewRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(universe_reviews).order_by(universe_reviews.c.id.desc()).limit(limit)
        ).all()
    return [_review(r) for r in rows]


def latest_done(engine: Engine, month: str | None = None) -> ReviewRow | None:
    q = select(universe_reviews).where(universe_reviews.c.status == "done")
    if month is not None:
        q = q.where(universe_reviews.c.month == month)
    with engine.connect() as conn:
        r = conn.execute(q.order_by(universe_reviews.c.id.desc()).limit(1)).first()
    return None if r is None else _review(r)


def evidence_by_id(engine: Engine, review_id: int) -> dict[int, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(universe_evidence).where(universe_evidence.c.review_id == review_id)
        ).all()
    out = {}
    for r in rows:
        d = dict(r._mapping)
        d["undated"] = d["date_source"] == "retrieved"
        out[int(r.id)] = d
    return out


def candidates(engine: Engine, review_id: int) -> list[CandidateRow]:
    ev = evidence_by_id(engine, review_id)
    with engine.connect() as conn:
        rows = conn.execute(
            select(universe_candidates).where(universe_candidates.c.review_id == review_id)
        ).all()
    out: list[CandidateRow] = []
    for r in rows:
        ids = json.loads(r.evidence_ids)
        out.append(
            CandidateRow(
                symbol=r.symbol,
                action=r.action,
                proposed_action=r.proposed_action,
                name=r.name,
                cik=r.cik,
                description=r.description,
                criteria=json.loads(r.criteria),
                overlap=json.loads(r.overlap),
                reasons=json.loads(r.reasons),
                gate_note=r.gate_note,
                evidence=[{"ref": f"U{i}", **ev[i]} for i in ids if i in ev],
            )
        )
    out.sort(key=lambda c: (ACTION_ORDER.get(c.action, 9), c.symbol))
    return out


def sweep_evidence(engine: Engine, review_id: int) -> dict[str, dict[str, Any]]:
    return {f"U{i}": e for i, e in evidence_by_id(engine, review_id).items() if e["symbol"] is None}


def pack_section(engine: Engine, month: str) -> dict[str, Any]:
    """The review pack's M12 section: this month's latest review (done, else its status)."""
    with engine.connect() as conn:
        r = conn.execute(
            select(universe_reviews)
            .where(universe_reviews.c.month == month)
            .order_by((universe_reviews.c.status == "done").desc(), universe_reviews.c.id.desc())
            .limit(1)
        ).first()
    if r is None:
        return {"status": "none"}
    rv = _review(r)
    if rv.status != "done":
        return {"status": rv.status, "as_of": rv.as_of, "error": rv.error}
    rows = candidates(engine, rv.id)
    return {
        "status": "done",
        "as_of": rv.as_of,
        "review_id": rv.id,
        "proposals": [
            {
                "symbol": c.symbol,
                "action": c.action,
                "name": c.name,
                "note": c.gate_note,
            }
            for c in rows
            if c.action in ("add", "remove", "watch")
        ],
    }
