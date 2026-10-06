"""Read-only views of conclusions and the track record (dashboard, weekly brief, review pack).
Runs on the dashboard's `mode=ro` engine; never writes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, select

from aether.config import TrackRecordParams
from aether.db.models import conclusion_failures, conclusions
from aether.market import last_ok_finished
from aether.score import track_record as tr

THEME = "$THEME"
DISCLAIMER = "Personal research tool, not financial advice."
OVERLAP_NOTE = (
    "Every weekly conclusion that isn't a held update counts as a call, so consecutive calls on "
    "the same name have overlapping windows and their hits are correlated."
)


def _row(r: Any) -> dict[str, Any]:
    d = dict(r._mapping)
    d["payload"] = json.loads(d["payload"])
    d["evidence"] = json.loads(d["evidence"])
    d.pop("input_hash", None)
    return d


def _key(d: Mapping[str, Any]) -> str:
    return d["symbol"] if d["kind"] == "ticker" else THEME


def latest_conclusions(engine: Engine) -> dict[str, dict[str, Any]]:
    """The latest conclusion per ticker, plus the theme under `$THEME`."""
    with engine.connect() as conn:
        rows = conn.execute(select(conclusions).order_by(conclusions.c.id)).all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _row(r)
        out[_key(d)] = d
    return out


def _as_of_or_before(engine: Engine, day: str) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                conclusions.c.kind,
                conclusions.c.symbol,
                conclusions.c.stance,
                conclusions.c.confidence,
                conclusions.c.as_of,
            )
            .where(conclusions.c.as_of <= day)
            .order_by(conclusions.c.id)
        ).all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = dict(r._mapping)
        out[_key(d)] = d
    return out


def stance_table(engine: Engine, p: TrackRecordParams, today: date) -> list[dict[str, Any]]:
    """Stance, confidence, the change vs a week ago, and the track-record standing."""
    latest = latest_conclusions(engine)
    week_ago = _as_of_or_before(engine, (today - timedelta(days=7)).isoformat())
    with engine.connect() as conn:
        stands = tr.standings(conn, p)
    out = []
    for key in sorted(latest, key=lambda k: (k == THEME, k == "QTUM", k)):
        c = latest[key]
        prev = week_ago.get(key)
        st = stands.get(key) or tr.standing([], p)
        out.append(
            {
                "key": key,
                "symbol": c["symbol"],
                "kind": c["kind"],
                "id": c["id"],
                "as_of": c["as_of"],
                "stance": c["stance"],
                "proposed": c["proposed_stance"],
                "held": bool(c["held"]),
                "hold_reason": c["hold_reason"],
                "confidence": c["confidence"],
                "horizon": c["horizon"],
                "verdict": c["payload"].get("one_line_verdict"),
                "label": c["payload"].get("label"),
                "prev_stance": prev["stance"] if prev else None,
                "conf_delta": None if prev is None else c["confidence"] - prev["confidence"],
                "changed": prev is not None and prev["stance"] != c["stance"],
                "standing": st,
            }
        )
    return out


def diff(prev: Mapping[str, Any] | None, cur: Mapping[str, Any]) -> dict[str, Any] | None:
    """What changed vs the previous conclusion: stance, confidence, thesis points."""
    if prev is None:
        return None
    old = {pt["point"] for pt in prev["payload"].get("thesis", [])}
    new = {pt["point"] for pt in cur["payload"].get("thesis", [])}
    return {
        "stance": None if prev["stance"] == cur["stance"] else (prev["stance"], cur["stance"]),
        "confidence": cur["confidence"] - prev["confidence"],
        "added": [pt for pt in cur["payload"].get("thesis", []) if pt["point"] not in old],
        "removed": sorted(old - new),
    }


def ticker_conclusions(engine: Engine, symbol: str | None, limit: int = 20) -> dict[str, Any]:
    """The current conclusion with its citations, and the history (newest first) with diffs."""
    with engine.connect() as conn:
        q = select(conclusions).order_by(conclusions.c.id.desc()).limit(limit + 1)
        q = (
            q.where(conclusions.c.symbol == symbol)
            if symbol
            else q.where(conclusions.c.kind == "theme")
        )
        rows = [_row(r) for r in conn.execute(q)]
        fails = conn.execute(
            select(conclusion_failures)
            .where(
                conclusion_failures.c.symbol == symbol
                if symbol
                else conclusion_failures.c.kind == "theme"
            )
            .order_by(conclusion_failures.c.id.desc())
            .limit(3)
        ).all()
    history = []
    for i, r in enumerate(rows[:limit]):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        history.append({**r, "diff": diff(prev, r)})
    return {
        "current": history[0] if history else None,
        "history": history,
        "failures": [dict(f._mapping) for f in fails],
    }


def citation(evidence: Mapping[str, Any], eid: str) -> dict[str, Any]:
    """Where a citation links: events → the feed, catalysts → /catalysts, facts → /facts, the
    rest → anchors on the ticker page."""
    e = dict(evidence.get(eid) or {"type": "unknown", "label": eid})
    t = e.get("type")
    if t == "event":
        e["href"] = f"/feed?event={e['ref']}"
    elif t == "catalyst":
        e["href"] = f"/catalysts#c{e['ref']}"
    elif t == "fact":
        e["href"] = f"/facts#{e['ref']}"
    elif t == "reaction":
        e["href"] = "#reactions"
    elif t == "score":
        e["href"] = "#scorecard"
    elif t == "options":
        e["href"] = "#options"
    elif t == "theme":
        e["href"] = "/#theme"
    elif t == "track":
        e["href"] = "/track-record"
    elif t == "stance":
        e["href"] = f"/t/{e['ref']}"
    e["id"] = eid
    return e


def track_record_page(engine: Engine, p: TrackRecordParams) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = tr.outcome_rows_for(conn)
        theme_rows = tr.outcome_rows_for(conn, kind="theme")
        stands = tr.standings(conn, p)
        overlay = tr.overlay_value(conn)
        fails = conn.execute(
            select(conclusion_failures).order_by(conclusion_failures.c.id.desc()).limit(10)
        ).all()
    per_stance = {
        s: tr.summarize([r for r in rows if r["stance"] == s], p)
        for s in ("ACCUMULATE", "HOLD", "TRIM", "AVOID")
    }
    symbols = sorted({r["symbol"] for r in rows})
    per_ticker = {s: tr.summarize([r for r in rows if r["symbol"] == s], p) for s in symbols}
    overall = tr.summarize(rows, p)
    six = overall[tr.PROVEN_HORIZON]
    if six["n"] == 0:
        verdict = "No track record yet."
    elif six["n"] < p.min_mature_calls:
        verdict = f"n too small (<{p.min_mature_calls} mature 6-month calls)."
    elif all(
        b is None or (six["hit_rate"] or 0) > b for b in (six["hold_rate"], six["momentum_rate"])
    ):
        verdict = "Aether beats both baselines at 6 months."
    else:
        verdict = "Aether does not beat the baselines at 6 months."
    return {
        "overall": overall,
        "verdict": verdict,
        "per_stance": per_stance,
        "per_ticker": per_ticker,
        "standings": stands,
        "calibration": tr.calibration(rows, p.confidence_buckets),
        "theme": tr.summarize(theme_rows, p),
        "overlay": overlay,
        "failures": [dict(f._mapping) for f in fails],
        "horizons": list(tr.HORIZON_MONTHS),
        "last_ok": last_ok_finished(engine, "track_record"),
        "overlap_note": OVERLAP_NOTE,
    }


def stance_line(r: Mapping[str, Any]) -> str:
    """One plain line for Telegram (brief, review pack): no model text, only computed fields."""
    who = "Theme tilt" if r["kind"] == "theme" else r["symbol"]
    held = f" (proposed {r['proposed']}, held)" if r["held"] else ""
    st = r["standing"]
    if st.n == 0:
        rec = st.label
    else:
        rec = f"{'' if st.proven else 'unproven, '}6m hit rate {st.hit_rate:.0%} (n={st.n})"
    return f"- {who}: {r['stance']}{held}, confidence {r['confidence']:.2f}; {rec}"


def briefs_view(engine: Engine, limit: int = 26) -> list[dict[str, Any]]:
    from aether.db.models import briefs

    with engine.connect() as conn:
        rows = conn.execute(select(briefs).order_by(briefs.c.as_of.desc()).limit(limit)).all()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def overlay_line(engine: Engine) -> str | None:
    """Layer 3 verdict for the selected profile (Holdings page)."""
    from aether.portfolio.holdings import load_settings

    profile = load_settings(engine).selected_profile
    with engine.connect() as conn:
        ov = tr.overlay_value(conn).get(profile)
    return None if ov is None else str(ov["verdict"])
