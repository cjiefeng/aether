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

M9 haircuts, from XBRL fundamentals as public on the publish date (`score/fundamentals.py`):
- fully diluted shares up more than `dilution_yoy_max` year on year (base dated on or after the
  stock's first session)                                                  -> x dilution_multiplier
- cash runway under `runway_min_months`                                   -> x runway_multiplier
They cite the filing behind the latest figure and carry the computed numbers in `detail`.

The owner can clear a reviewed filing by accession (`overlay.cleared_accessions`).

M10 layer 2: each pure-play's latest stance (after hysteresis, not older than
`conclusions.stance_max_age_days`) scales its weight by `overlay.stance_multipliers`; while the
ticker's track record is unproven (score/track_record.py) the multiplier is clamped to
`overlay.unproven_clamp`. Hard rules are never clamped. Every adjustment cites its evidence
(event, filing or conclusion id); options analytics and valuation never enter the overlay.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Connection, func, select

from aether.config import OverlayParams, TrackRecordParams
from aether.db.models import conclusions, events, filings, prices_daily
from aether.score import fundamentals as fnd
from aether.score import track_record as tr

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
    "fd_dilution": "fully diluted shares up YoY",
    "low_runway": "cash runway below minimum",
    "stance": "stance multiplier",
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
    detail: dict[str, Any] | None = field(default=None, compare=False, hash=False)

    def evidence(self) -> dict[str, Any]:
        label = (self.detail or {}).get("label") or RULE_LABELS[self.rule]
        return {
            "rule": self.rule,
            "label": label,
            "multiplier": self.multiplier,
            "event_id": self.event_id,
            "accession": self.accession,
            "form": self.form,
            "filed_at": self.filed_at,
            "url": self.url,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------- pure


def _r(x: float) -> float:
    return round(x, DP) + 0.0


@dataclass(frozen=True)
class StanceAdj:
    """Layer 2 (M10): one pure-play's stance multiplier after the earned-trust clamp."""

    symbol: str
    stance: str
    raw: float  # the configured multiplier for the stance
    applied: float  # after the clamp
    clamped: bool
    conclusion_id: int
    as_of: str
    trust: str  # the track-record standing label

    def evidence(self, weight: float) -> dict[str, Any]:
        label = f"{self.stance} × {self.raw:g}"  # noqa: RUF001
        if self.clamped:
            label += f" → clamped × {self.applied:g} (unproven)"  # noqa: RUF001
        return {
            "rule": "stance",
            "label": label,
            "multiplier": self.applied,
            "raw_multiplier": self.raw,
            "clamped": self.clamped,
            "conclusion_id": self.conclusion_id,
            "conclusion_as_of": self.as_of,
            "trust": self.trust,
            "event_id": None,
            "accession": None,
            "form": None,
            "filed_at": None,
            "url": None,
            "detail": None,
            "weight": _r(weight),
        }


def apply_overlay(
    base: Mapping[str, float],
    sleeve: Iterable[str],
    cap: float,
    findings: Iterable[Finding],
    stances: Iterable[StanceAdj] = (),
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Published weights and the per-name adjustment chain.

    base → layer 1 (filing rules) → layer 2 (stance multipliers) → caps → redistribution:
    - names cut by either layer (a multiplier < 1) never receive freed weight;
    - if the stances push the sleeve above its base total, the uncut names are scaled back pro
      rata to their adjusted weights, so QTUM's fixed weight is never squeezed;
    - weight over a cap, or freed by cuts, goes to the uncut names pro rata within caps; the
      remainder goes to QTUM.
    """
    sleeve = sorted(set(sleeve))
    hits: dict[str, list[Finding]] = {}
    for f in findings:
        if f.symbol in sleeve:
            hits.setdefault(f.symbol, []).append(f)
    by_stance = {a.symbol: a for a in stances if a.symbol in sleeve}

    w = {s: float(base.get(s, 0.0)) for s in sorted(set(base) | set(sleeve) | {CORE})}
    steps: dict[str, list[dict[str, Any]]] = {s: [] for s in w}
    reduced: set[str] = set()
    for s in sleeve:
        for f in sorted(hits.get(s, []), key=lambda f: (f.rule, f.filed_at, f.accession)):
            w[s] = w[s] * f.multiplier
            steps[s].append({**f.evidence(), "weight": _r(w[s])})
            if f.multiplier < 1:
                reduced.add(s)
        a = by_stance.get(s)
        if a is not None and w[s] > EPS:
            w[s] = w[s] * a.applied
            steps[s].append(a.evidence(w[s]))
            if a.applied < 1:
                reduced.add(s)

    redistributed = {s: 0.0 for s in w}
    budget = sum(float(base.get(s, 0.0)) for s in sleeve)
    excess = sum(w[s] for s in sleeve) - budget
    if excess > EPS:
        up = [s for s in sleeve if s not in reduced and w[s] > EPS]
        total = sum(w[s] for s in up)
        scale = max(0.0, (total - excess) / total) if total > EPS else 1.0
        for s in up:
            redistributed[s] -= w[s] - w[s] * scale
            w[s] *= scale
    freed = max(0.0, -excess)
    # Layer 2 can't lift a name over its cap (the base itself already respects it).
    for s in sleeve:
        limit = max(cap, float(base.get(s, 0.0)))
        if w[s] > limit + EPS:
            freed += w[s] - limit
            redistributed[s] -= w[s] - limit
            w[s] = limit

    # Freed weight → the uncut, still held pure-plays pro rata, within caps.
    open_ = [s for s in sleeve if s not in reduced and w[s] > EPS and w[s] < cap - EPS]
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
        if st.get("conclusion_id"):
            ref = f"conclusion #{st['conclusion_id']}"
        elif st["event_id"]:
            ref = f"event #{st['event_id']}"
        else:
            ref = f"{st['form'] or 'XBRL'} {st['accession']}"
        parts.append(f"{st['label']} ({ref}) → {st['weight'] * 100:.1f}%")
    if abs(c["redistributed"]) > 1e-9:
        sign = "+" if c["redistributed"] > 0 else "-"
        parts.append(f"{sign}{abs(c['redistributed']) * 100:.1f}% redistributed")
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
    out += fundamentals_findings(conn, syms, as_of, params, cleared)
    # One finding per (symbol, rule): the most recent filing is the evidence.
    best: dict[tuple[str, str], Finding] = {}
    for f in out:
        k = (f.symbol, f.rule)
        if k not in best or (f.filed_at, f.accession) > (best[k].filed_at, best[k].accession):
            best[k] = f
    return [best[k] for k in sorted(best)]


def _filing(conn: Connection, accession: str | None) -> tuple[str, str]:
    """(filed_at, url) of a stored filing, else empty strings."""
    if not accession:
        return "", ""
    r = conn.execute(
        select(filings.c.filed_at, filings.c.url).where(filings.c.accession == accession)
    ).first()
    return (r.filed_at, r.url) if r else ("", "")


def fundamentals_findings(
    conn: Connection, syms: Iterable[str], as_of: date, params: OverlayParams, cleared: set[str]
) -> list[Finding]:
    """M9 haircuts: fully diluted dilution and cash runway, point in time at `as_of`."""
    out: list[Finding] = []
    for s in syms:
        snap = fnd.snapshot(conn, s, as_of)
        yoy = snap.fd_yoy.get("value")
        if yoy is not None and yoy > params.dilution_yoy_max:
            to = snap.fd_yoy["to"]["common"]
            acc = to["accession"] or ""
            if acc not in cleared:
                filed, url = _filing(conn, acc)
                out.append(
                    Finding(
                        s,
                        "fd_dilution",
                        params.dilution_multiplier,
                        acc,
                        to["form"] or "",
                        filed or to["period_end"],
                        url,
                        None,
                        {
                            "label": f"FD shares {yoy * 100:+.1f}% YoY",
                            "yoy": round(yoy, 6),
                            "from_shares": snap.fd_yoy["from"]["total"],
                            "from_date": snap.fd_yoy["from"]["common"]["period_end"],
                            "to_shares": snap.fd_yoy["to"]["total"],
                            "to_date": to["period_end"],
                            "components": snap.fd_yoy["components_compared"],
                        },
                    )
                )
        if snap.runway_months is not None and snap.runway_months < params.runway_min_months:
            assert snap.ocf_ttm is not None
            ref = snap.ocf_ttm.refs[-1] if snap.ocf_ttm.method == "fy" else snap.ocf_ttm.refs[1]
            acc = ref["accession"] or ""
            if acc not in cleared:
                filed, url = _filing(conn, acc)
                out.append(
                    Finding(
                        s,
                        "low_runway",
                        params.runway_multiplier,
                        acc,
                        ref["form"] or "",
                        filed or ref["period_end"],
                        url,
                        None,
                        {
                            "label": f"cash runway {snap.runway_months:.1f} months",
                            "runway_months": round(snap.runway_months, 4),
                            "liquidity": float(snap.liquidity or 0),
                            "quarterly_burn": float(snap.quarterly_burn or 0),
                            "ocf_ttm_end": snap.ocf_ttm.end.isoformat(),
                        },
                    )
                )
    return out


# --------------------------------------------------------------------------- layer 2 (reads)


def stance_adjustments(
    conn: Connection,
    symbols: Iterable[str],
    as_of: date,
    params: OverlayParams,
    max_age_days: int,
    track: TrackRecordParams,
) -> list[StanceAdj]:
    """The latest ticker conclusion per pure-play, as of `as_of`, if recent enough."""
    if not params.enabled:
        return []
    since = (as_of - timedelta(days=max_age_days)).isoformat()
    lo, hi = params.unproven_clamp
    out = []
    for s in sorted(set(symbols) - {CORE}):
        r = conn.execute(
            select(conclusions.c.id, conclusions.c.stance, conclusions.c.as_of)
            .where(
                conclusions.c.kind == "ticker",
                conclusions.c.symbol == s,
                conclusions.c.as_of <= as_of.isoformat(),
            )
            .order_by(conclusions.c.as_of.desc(), conclusions.c.id.desc())
            .limit(1)
        ).first()
        if r is None or r.as_of < since:
            continue
        st = tr.standing(tr.outcome_rows_for(conn, s), track)
        raw = params.stance_multipliers[r.stance]
        applied = raw if st.proven else min(max(raw, lo), hi)
        out.append(StanceAdj(s, r.stance, raw, applied, applied != raw, r.id, r.as_of, st.label))
    return out


def name_overlay(
    conn: Connection,
    symbol: str,
    as_of: date,
    params: OverlayParams,
    max_age_days: int,
    track: TrackRecordParams,
) -> list[list[Any]]:
    """What the overlay would do to one name at a publish on `as_of`: its layer-1 rules and its
    layer-2 stance multiplier, as a comparable list (M13: an escalation result is sent only when
    this changes)."""
    out: list[list[Any]] = [
        [f.rule, _r(f.multiplier)] for f in layer1_findings(conn, [symbol], as_of, params)
    ]
    out += [
        ["stance", a.stance, _r(a.applied)]
        for a in stance_adjustments(conn, [symbol], as_of, params, max_age_days, track)
    ]
    return sorted(out)
