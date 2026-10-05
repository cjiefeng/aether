"""Catalyst sync + resolution (spec §11 M8). DB-only and deterministic; no network, no LLM.

One pass (`refresh_catalysts`), in one short `write_tx`:

1. **Desired rows** from three origins, keyed by `catalysts.key`:
   - `seed:<id>`: `config/catalysts_seed.yaml` (each linked to a fact id);
   - `earnings:<SYM>:<date>`: every `earnings_calendar` row;
   - `lockup:<accession>`: every `lockups` row up to `lockup_lookahead_days` ahead, unless a seed
     lock-up already covers the same symbol and date.
   Definitions are updated in place; status and resolution are never touched by the sync, except
   that a future earnings date that left the calendar becomes `cancelled` (`rescheduled`).
2. **Resolution** of `upcoming` rows (catalysts/resolve.py rules), each citing an event or a rule.

Only rows that actually change are written, so an idle run writes nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update

from aether.catalysts.resolve import Resolution, resolve_upcoming
from aether.config import CatalystsConfig
from aether.db.engine import write_tx
from aether.db.models import catalysts, earnings_calendar, filings, lockups
from aether.db.types import utcnow_iso
from aether.runs import JobResult

DEFINITION_COLS = (
    "origin",
    "symbol",
    "title",
    "kind",
    "window_start",
    "window_end",
    "fact_id",
    "source_url",
    "keywords",
    "resolve_categories",
)


def _seed_rows(cfg: CatalystsConfig) -> list[dict[str, Any]]:
    return [
        {
            "key": f"seed:{c.id}",
            "origin": "seed",
            "symbol": c.symbol,
            "title": c.title,
            "kind": c.kind,
            "window_start": c.window_start,
            "window_end": c.window_end,
            "fact_id": c.fact_id,
            "source_url": c.source_url,
            "keywords": json.dumps(list(c.keywords)),
            "resolve_categories": json.dumps(list(c.resolve_categories)),
        }
        for c in cfg.catalysts
    ]


def _earnings_rows(conn: Connection) -> list[dict[str, Any]]:
    out = []
    for sym, d, status, url in conn.execute(
        select(
            earnings_calendar.c.symbol,
            earnings_calendar.c.date,
            earnings_calendar.c.status,
            earnings_calendar.c.source_url,
        )
    ):
        out.append(
            {
                "key": f"earnings:{sym}:{d}",
                "origin": "earnings",
                "symbol": sym,
                "title": f"{sym} earnings ({'reported' if status == 'reported' else 'scheduled'})",
                "kind": "earnings",
                "window_start": d,
                "window_end": d,
                "fact_id": None,
                "source_url": url,
                "keywords": "[]",
                "resolve_categories": "[]",
            }
        )
    return out


def _lockup_rows(
    conn: Connection, cfg: CatalystsConfig, today: date, seeds: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    covered = {(s["symbol"], s["window_start"]) for s in seeds if s["kind"] == "lockup"}
    horizon = (today + timedelta(days=cfg.rules.lockup_lookahead_days)).isoformat()
    out = []
    for acc, sym, expiry, days, url in conn.execute(
        select(
            lockups.c.accession,
            lockups.c.symbol,
            lockups.c.expiry_date,
            lockups.c.lockup_days,
            filings.c.url,
        )
        .join(filings, filings.c.accession == lockups.c.accession)
        .where(lockups.c.expiry_date <= horizon)
    ):
        if (sym, expiry) in covered:
            continue
        out.append(
            {
                "key": f"lockup:{acc}",
                "origin": "lockup",
                "symbol": sym,
                "title": f"{sym} lock-up ends ({days} days after the prospectus)",
                "kind": "lockup",
                "window_start": expiry,
                "window_end": expiry,
                "fact_id": None,
                "source_url": url,
                "keywords": "[]",
                "resolve_categories": "[]",
            }
        )
    return out


@dataclass(frozen=True)
class SyncSummary:
    inserted: int
    updated: int
    rescheduled: int
    resolved: list[Resolution]

    @property
    def changed(self) -> int:
        return self.inserted + self.updated + self.rescheduled + len(self.resolved)


def refresh_catalysts(engine: Engine, cfg: CatalystsConfig, today: date) -> SyncSummary:
    now = utcnow_iso()
    t = today.isoformat()
    with write_tx(engine) as conn:
        seeds = _seed_rows(cfg)
        earnings = _earnings_rows(conn)
        desired = {r["key"]: r for r in [*seeds, *earnings, *_lockup_rows(conn, cfg, today, seeds)]}
        existing = {
            r.key: r
            for r in conn.execute(
                select(
                    catalysts.c.id,
                    catalysts.c.key,
                    catalysts.c.status,
                    *[catalysts.c[c] for c in DEFINITION_COLS],
                )
            )
        }
        inserted = updated = rescheduled = 0
        for key, row in desired.items():
            cur = existing.get(key)
            if cur is None:
                conn.execute(insert(catalysts).values(**row, status="upcoming", updated_at=now))
                inserted += 1
            elif any(getattr(cur, c) != row[c] for c in DEFINITION_COLS):
                conn.execute(
                    update(catalysts)
                    .where(catalysts.c.id == cur.id)
                    .values(**{c: row[c] for c in DEFINITION_COLS}, updated_at=now)
                )
                updated += 1
        # A future earnings date that left the calendar was moved (the calendar job replaces
        # scheduled dates on every run).
        for key, cur in existing.items():
            if (
                cur.origin == "earnings"
                and key not in desired
                and cur.status == "upcoming"
                and cur.window_start >= t
            ):
                conn.execute(
                    update(catalysts)
                    .where(catalysts.c.id == cur.id)
                    .values(
                        status="cancelled",
                        resolution="rescheduled",
                        resolved_at=now,
                        note="date no longer in the earnings calendar",
                        updated_at=now,
                    )
                )
                rescheduled += 1
        resolved = resolve_upcoming(conn, cfg.rules, today)
        for r in resolved:
            conn.execute(
                update(catalysts)
                .where(catalysts.c.id == r.catalyst_id, catalysts.c.status == "upcoming")
                .values(
                    status=r.status,
                    resolution=r.resolution,
                    resolved_by_event_id=r.event_id,
                    resolved_at=now,
                    note=r.note,
                    updated_at=now,
                )
            )
    return SyncSummary(inserted, updated, rescheduled, resolved)


def catalysts_job(engine: Engine, cfg: CatalystsConfig, today: date) -> JobResult:
    s = refresh_catalysts(engine, cfg, today)
    return JobResult(rows_written=s.changed)
