"""Research overlay, layer 1: filing hard rules (spec §6.6.1, M5). Deterministic, no LLM.

    base weights (selected sleeve method, §6.5)
      → layer 1: hard rules from filings     (zero a name)
      → per-name caps; freed weight to the other pure-plays pro rata to their weights, within
        caps; any remainder to QTUM
      → published target

Rules (T1 filings by the pure-play itself; a finding whose event is quarantined is ignored):
- going concern in the latest 10-K/10-Q                                   -> x 0
- 8-K Item 3.01 whose text is a deficiency notice (not a voluntary
  exchange transfer), for N days                                          -> x 0
- delisted / acquired: a Form 25, 25-NSE, 15-12B or 15-12G covering the
  common stock (not warrants, units or notes) AND the stock has stopped
  trading (no close for N sessions while QTUM trades)                     -> x 0

Tightened after the live check (owner decision 2026-10-05): real filings showed warrant
delistings, voluntary exchange transfers and a de-SPAC 8-K 5.01 that the plain form/item rules
would have read as permanent loss. 8-K 5.01 only alerts.

The owner can clear a reviewed filing by accession (`overlay.cleared_accessions`). Layer-1
dilution/runway haircuts arrive in M9 and stance multipliers in M10. Every adjustment cites its
evidence; options analytics and valuation never enter the overlay.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Connection, func, select

from aether.config import OverlayParams
from aether.db.models import events, filings, prices_daily

CORE = "QTUM"
PERIODIC = ("10-K", "10-Q", "10-K/A", "10-Q/A")
EIGHT_K = ("8-K", "8-K/A")
DELISTING_FORMS = ("25", "25-NSE", "15-12B", "15-12G")
EPS = 1e-12
DP = 10

RULE_LABELS = {
    "going_concern": "going concern",
    "compliance_notice": "listing-deficiency notice (8-K 3.01)",
    "acquired_or_delisted": "acquired / delisted",
}


@dataclass(frozen=True)
class Finding:
    symbol: str
    rule: str
    multiplier: float
    accession: str
    form: str
    filed_at: str
    url: str
    event_id: int | None

    def evidence(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "label": RULE_LABELS[self.rule],
            "multiplier": self.multiplier,
            "event_id": self.event_id,
            "accession": self.accession,
            "form": self.form,
            "filed_at": self.filed_at,
            "url": self.url,
        }


# --------------------------------------------------------------------------- pure


def _r(x: float) -> float:
    return round(x, DP) + 0.0


def apply_overlay(
    base: Mapping[str, float],
    sleeve: Iterable[str],
    cap: float,
    findings: Iterable[Finding],
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Published weights and the per-name adjustment chain."""
    sleeve = sorted(set(sleeve))
    hits: dict[str, list[Finding]] = {}
    for f in findings:
        if f.symbol in sleeve:
            hits.setdefault(f.symbol, []).append(f)

    w = {s: float(base.get(s, 0.0)) for s in sorted(set(base) | set(sleeve) | {CORE})}
    steps: dict[str, list[dict[str, Any]]] = {s: [] for s in w}
    freed = 0.0
    for s in sleeve:
        for f in sorted(hits.get(s, []), key=lambda f: (f.rule, f.filed_at, f.accession)):
            new = w[s] * f.multiplier
            steps[s].append({**f.evidence(), "weight": _r(new)})
            freed += w[s] - new
            w[s] = new

    # Freed weight → the other (unaffected, still held) pure-plays pro rata, within caps.
    redistributed = {s: 0.0 for s in w}
    open_ = [s for s in sleeve if s not in hits and w[s] > EPS and w[s] < cap - EPS]
    for _ in range(len(sleeve) + 1):
        if freed <= EPS or not open_:
            break
        total = sum(w[s] for s in open_)
        given = 0.0
        for s in open_:
            add = min(freed * w[s] / total, cap - w[s])
            w[s] += add
            redistributed[s] += add
            given += add
        freed -= given
        open_ = [s for s in open_ if w[s] < cap - EPS]
    if freed > EPS:
        w[CORE] = w.get(CORE, 0.0) + freed
        redistributed[CORE] += freed

    published = {s: _r(v) for s, v in sorted(w.items()) if _r(v) > 0}
    chain = [
        {
            "symbol": s,
            "base": _r(float(base.get(s, 0.0))),
            "steps": steps[s],
            "redistributed": _r(redistributed[s]),
            "final": published.get(s, 0.0),
        }
        for s in sorted(w)
        if base.get(s, 0.0) > 0 or published.get(s, 0.0) > 0 or steps[s]
    ]
    return published, chain


def chain_text(c: Mapping[str, Any]) -> str:
    """e.g. `base 14.0% → going concern (event #812) → 0.0%`."""
    parts = [f"base {c['base'] * 100:.1f}%"]
    for st in c["steps"]:
        ref = f"event #{st['event_id']}" if st["event_id"] else f"{st['form']} {st['accession']}"
        parts.append(f"{st['label']} ({ref}) → {st['weight'] * 100:.1f}%")
    if abs(c["redistributed"]) > 1e-9:
        parts.append(f"+{c['redistributed'] * 100:.1f}% redistributed")
    if len(parts) > 1:
        parts.append(f"{c['final'] * 100:.1f}%")
    return " → ".join(parts)


# --------------------------------------------------------------------------- findings (reads)


def stopped_trading(conn: Connection, symbol: str, day: str, sessions: int) -> bool:
    """True if QTUM has traded at least `sessions` sessions (up to `day`) since the symbol's
    last close: the stock is no longer trading, not just moving exchanges."""
    last = conn.execute(
        select(func.max(prices_daily.c.d)).where(
            prices_daily.c.symbol == symbol, prices_daily.c.d <= day
        )
    ).scalar()
    q = select(func.count()).where(prices_daily.c.symbol == CORE, prices_daily.c.d <= day)
    if last is not None:
        q = q.where(prices_daily.c.d > last)
    return int(conn.execute(q).scalar_one()) >= sessions


def _event_for(conn: Connection, accession: str) -> tuple[bool, int | None]:
    """(usable, event id). A filing whose only event is quarantined isn't usable."""
    rows = conn.execute(
        select(events.c.id, events.c.quarantined)
        .where(events.c.accession == accession)
        .order_by(events.c.id)
    ).all()
    ok = [r.id for r in rows if not r.quarantined]
    if rows and not ok:
        return False, None
    return True, ok[0] if ok else None


def layer1_findings(
    conn: Connection, symbols: Iterable[str], as_of: date, params: OverlayParams
) -> list[Finding]:
    """Hard-rule findings for filings on or before `as_of`."""
    syms = sorted(set(symbols) - {CORE})
    if not params.enabled or not syms:
        return []
    cleared = set(params.cleared_accessions)
    day = as_of.isoformat()
    since_notice = (as_of - timedelta(days=params.compliance_notice_days)).isoformat()
    out: list[Finding] = []

    def add(rule: str, r: Any) -> None:
        if r.accession in cleared:
            return
        usable, eid = _event_for(conn, r.accession)
        if usable:
            out.append(Finding(r.symbol, rule, 0.0, r.accession, r.form, r.filed_at, r.url, eid))

    cols = (
        filings.c.symbol,
        filings.c.accession,
        filings.c.form,
        filings.c.filed_at,
        filings.c.url,
    )
    # Going concern: the latest parsed 10-K/10-Q per symbol.
    latest = (
        select(filings.c.symbol, func.max(filings.c.filed_at).label("d"))
        .where(
            filings.c.symbol.in_(syms),
            filings.c.form.in_(PERIODIC),
            filings.c.parsed.is_not(None),
            filings.c.filed_at <= day,
        )
        .group_by(filings.c.symbol)
        .subquery()
    )
    for r in conn.execute(
        select(*cols, filings.c.parsed)
        .join(latest, (filings.c.symbol == latest.c.symbol) & (filings.c.filed_at == latest.c.d))
        .where(filings.c.form.in_(PERIODIC), filings.c.parsed.is_not(None))
        .order_by(filings.c.symbol, filings.c.accession)
    ):
        if json.loads(r.parsed).get("going_concern"):
            add("going_concern", r)

    for row in conn.execute(
        select(*cols, filings.c["items"], filings.c.parsed)
        .where(
            filings.c.symbol.in_(syms),
            filings.c.filed_at <= day,
            filings.c.form.in_((*EIGHT_K, *DELISTING_FORMS)),
        )
        .order_by(filings.c.symbol, filings.c.filed_at, filings.c.accession)
    ):
        items = json.loads(row._mapping["items"])
        summary = json.loads(row.parsed) if row.parsed else {}
        if row.form in DELISTING_FORMS:
            if summary.get("covers_common") is True and stopped_trading(
                conn, row.symbol, day, params.delisted_stale_sessions
            ):
                add("acquired_or_delisted", row)
        elif (
            "3.01" in items
            and row.filed_at >= since_notice
            and summary.get("listing_notice") == "deficiency"
        ):
            add("compliance_notice", row)
    # One finding per (symbol, rule): the most recent filing is the evidence.
    best: dict[tuple[str, str], Finding] = {}
    for f in out:
        k = (f.symbol, f.rule)
        if k not in best or (f.filed_at, f.accession) > (best[k].filed_at, best[k].accession):
            best[k] = f
    return [best[k] for k in sorted(best)]
