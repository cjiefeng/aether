"""Calibration report (spec §6.3) and implied vs realized moves (§6.8). Deterministic; no LLM.

Pools reaction rows that are `complete` (not pending, not confounded) and not `approx_time`, then
reports per class and per category: n, mean |z1| and |z5|, the share with |z5| > 2, the median
reversal ratio, mean abnormal volume and, for SIGNAL/RISK, the direction hit rate (sign of CAR[0,5]
vs the classifier's direction for that ticker; direction 0 is skipped). A category is reported
only with at least `min_n` events.

Flags suggest actions and are never applied:
- a NOISE category that behaves like signal (mean |z5| or share of |z5| > 2 above the thresholds)
  -> consider reclassifying;
- a SIGNAL category the market ignores (mean |z5| below the threshold) -> review its rubric/weight;
- events at or above `high_materiality` with |z5| below the threshold -> listed for spot review.

Implied vs realized: for each catalyst resolved by an event, the straddle-implied move from the
last options snapshot before t0 that priced it, next to the realized |CAR[0,1]| and raw |return|
over [t0-1 close, t0+1 close].
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from statistics import mean, median
from typing import Any

from sqlalchemy import Connection, Engine, func, select

from aether.config import CalibrationParams
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    calibration_reports,
    catalysts,
    event_classifications,
    event_reactions,
    event_tickers,
    events,
    options_snapshots,
)
from aether.db.types import utcnow_iso
from aether.runs import JobResult

DP = 6
SPOT_REVIEW_LIMIT = 50


def _r(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, DP) + 0.0


def stats(rows: Sequence[dict[str, Any]], directional: bool) -> dict[str, Any]:
    z1 = [abs(r["z_1"]) for r in rows if r["z_1"] is not None]
    z5 = [abs(r["z_5"]) for r in rows if r["z_5"] is not None]
    rev = [r["reversal_ratio"] for r in rows if r["reversal_ratio"] is not None]
    vol = [r["abn_volume"] for r in rows if r["abn_volume"] is not None]
    out: dict[str, Any] = {
        "n": len(rows),
        "mean_abs_z1": _r(mean(z1)) if z1 else None,
        "mean_abs_z5": _r(mean(z5)) if z5 else None,
        "pct_abs_z5_gt2": _r(sum(z > 2 for z in z5) / len(z5)) if z5 else None,
        "median_reversal": _r(median(rev)) if rev else None,
        "mean_abn_volume": _r(mean(vol)) if vol else None,
    }
    if directional:
        calls = [
            (r["direction"], r["car_5"])
            for r in rows
            if r["direction"] not in (None, 0) and r["car_5"] is not None
        ]
        hits = sum((car > 0) == (d > 0) for d, car in calls)
        out["direction_n"] = len(calls)
        out["direction_hit_rate"] = _r(hits / len(calls)) if calls else None
    return out


def build_report(rows: Sequence[dict[str, Any]], p: CalibrationParams) -> dict[str, Any]:
    usable = [r for r in rows if r["status"] == "complete" and not r["approx_time"]]
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_cat: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in usable:
        by_class[r["class"]].append(r)
        by_cat[(r["class"], r["category"])].append(r)

    classes = {
        c: stats(by_class.get(c, []), c in ("SIGNAL", "RISK")) for c in ("SIGNAL", "NOISE", "RISK")
    }
    categories = []
    flags = []
    for (cls, cat), rs in sorted(by_cat.items()):
        if len(rs) < p.min_n:
            categories.append({"class": cls, "category": cat, "n": len(rs), "too_few": True})
            continue
        st = stats(rs, cls in ("SIGNAL", "RISK"))
        categories.append({"class": cls, "category": cat, **st, "too_few": False})
        mz, pct = st["mean_abs_z5"], st["pct_abs_z5_gt2"]
        if cls == "NOISE" and (
            (mz is not None and mz >= p.noise_like_signal_mean_abs_z5)
            or (pct is not None and pct >= p.noise_like_signal_pct_z5_gt2)
        ):
            flags.append(
                {
                    "kind": "noise_like_signal",
                    "class": cls,
                    "category": cat,
                    "suggestion": "NOISE category moves like signal: consider reclassifying it",
                    "mean_abs_z5": mz,
                    "pct_abs_z5_gt2": pct,
                }
            )
        if cls == "SIGNAL" and mz is not None and mz < p.signal_ignored_mean_abs_z5:
            flags.append(
                {
                    "kind": "signal_ignored",
                    "class": cls,
                    "category": cat,
                    "suggestion": "the market ignores this SIGNAL category: review its rubric or "
                    "weight",
                    "mean_abs_z5": mz,
                    "pct_abs_z5_gt2": pct,
                }
            )
    spot = sorted(
        (
            {
                "event_id": r["event_id"],
                "symbol": r["symbol"],
                "title": r["title"],
                "class": r["class"],
                "category": r["category"],
                "materiality": r["materiality"],
                "t0": r["t0"],
                "z_5": r["z_5"],
            }
            for r in usable
            if r["materiality"] >= p.high_materiality
            and r["z_5"] is not None
            and abs(r["z_5"]) < p.high_materiality_max_abs_z5
        ),
        key=lambda x: (x["t0"], x["event_id"], x["symbol"]),
        reverse=True,
    )
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    return {
        "classes": classes,
        "categories": categories,
        "flags": flags,
        "spot_review": spot[:SPOT_REVIEW_LIMIT],
        "spot_review_total": len(spot),
        "counts": {
            **{k: counts[k] for k in ("complete", "pending", "confounded", "no_data")},
            "approx_time_excluded": sum(
                r["status"] == "complete" and bool(r["approx_time"]) for r in rows
            ),
            "usable": len(usable),
        },
        "params": p.model_dump(mode="json"),
    }


# --------------------------------------------------------------------------- reads


def load_rows(conn: Connection) -> list[dict[str, Any]]:
    q = (
        select(
            event_reactions,
            events.c.title,
            event_classifications.c["class"],
            event_classifications.c.category,
            event_classifications.c.materiality,
            event_classifications.c.direction.label("class_direction"),
            event_tickers.c.direction.label("ticker_direction"),
        )
        .join(events, events.c.id == event_reactions.c.event_id)
        .join(event_classifications, event_classifications.c.event_id == event_reactions.c.event_id)
        .outerjoin(
            event_tickers,
            (event_tickers.c.event_id == event_reactions.c.event_id)
            & (event_tickers.c.symbol == event_reactions.c.symbol),
        )
        .where(events.c.quarantined == 0)
        .order_by(event_reactions.c.event_id, event_reactions.c.symbol)
    )
    out = []
    for r in conn.execute(q):
        m = dict(r._mapping)
        td = m.pop("ticker_direction")
        cd = m.pop("class_direction")
        m["direction"] = td if td is not None else cd
        out.append(m)
    return out


def implied_vs_realized(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(
            catalysts.c.id,
            catalysts.c.symbol,
            catalysts.c.title,
            catalysts.c.kind,
            catalysts.c.status,
            catalysts.c.resolved_by_event_id,
            event_reactions.c.t0,
            event_reactions.c.car_1,
            event_reactions.c.ret_raw_1,
            event_reactions.c.status.label("reaction_status"),
        )
        .join(
            event_reactions,
            (event_reactions.c.event_id == catalysts.c.resolved_by_event_id)
            & (event_reactions.c.symbol == catalysts.c.symbol),
        )
        .where(catalysts.c.resolved_by_event_id.is_not(None), event_reactions.c.t0.is_not(None))
        .order_by(event_reactions.c.t0.desc(), catalysts.c.id)
    ).all()
    out = []
    for r in rows:
        snaps = conn.execute(
            select(options_snapshots.c.d, options_snapshots.c.metrics)
            .where(options_snapshots.c.symbol == r.symbol, options_snapshots.c.d < r.t0)
            .order_by(options_snapshots.c.d.desc())
            .limit(10)
        ).all()
        implied = None
        for s in snaps:
            for m in json.loads(s.metrics).get("implied_moves", []):
                if m.get("catalyst_id") == r.id and m.get("move") is not None:
                    implied = {"snapshot": s.d, "move": m["move"], "expiry": m.get("expiry")}
                    break
            if implied:
                break
        if implied is None:
            continue
        real_car = abs(r.car_1) if r.car_1 is not None else None
        real_raw = abs(r.ret_raw_1) if r.ret_raw_1 is not None else None
        out.append(
            {
                "catalyst_id": r.id,
                "symbol": r.symbol,
                "title": r.title,
                "kind": r.kind,
                "status": r.status,
                "event_id": r.resolved_by_event_id,
                "t0": r.t0,
                "reaction_status": r.reaction_status,
                "implied_move": implied["move"],
                "snapshot": implied["snapshot"],
                "expiry": implied["expiry"],
                "realized_abs_car_1": _r(real_car),
                "realized_abs_return_1": _r(real_raw),
                "ratio": _r(real_raw / implied["move"])
                if real_raw is not None and implied["move"]
                else None,
            }
        )
    return out


def run_calibration(engine: Engine, p: CalibrationParams, today: date) -> JobResult:
    with engine.connect() as conn:
        report = build_report(load_rows(conn), p)
        report["implied_vs_realized"] = implied_vs_realized(conn)
    report["as_of"] = today.isoformat()
    with write_tx(engine) as conn:
        upsert(
            conn,
            calibration_reports,
            [
                {
                    "as_of": today.isoformat(),
                    "payload": json.dumps(report, sort_keys=True),
                    "created_at": utcnow_iso(),
                }
            ],
            key_cols=["as_of"],
        )
    return JobResult(rows_written=1)


def has_report(engine: Engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(select(func.count()).select_from(calibration_reports)).scalar())
