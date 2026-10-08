"""Structural removal triggers for current pure-plays (spec §6.7 "Removal triggers": delisted or
acquired, 8-K Items 2.01 / 3.01, or a closed merger). Read-only, deterministic.

- the overlay's `acquired_or_delisted` finding (Form 25/15 covering the common stock, and the
  stock has stopped trading) and its `compliance_notice` (8-K Item 3.01 read as a deficiency
  notice, within the overlay's notice window): `portfolio/overlay.py::layer1_findings`;
- an 8-K reporting **both** Item 2.01 (completion of an acquisition or disposition) **and** Item
  5.01 (change in control of the registrant) within the lookback: the registrant itself was
  acquired. Item 2.01 alone isn't used: an acquirer files it too (IonQ's own 8-K closing the
  SkyWater acquisition would read as IonQ being acquired).

Quarantined filing events and owner-cleared accessions are ignored, as in the overlay.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import Engine, select

from aether.config import OverlayParams
from aether.db.models import filings
from aether.portfolio.overlay import EIGHT_K, _event_for, layer1_findings

STRUCTURAL_RULES = ("acquired_or_delisted", "compliance_notice")
CHANGE_OF_CONTROL_DAYS = 400


def structural_triggers(
    engine: Engine, symbols: Sequence[str], as_of: date, params: OverlayParams
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {s: [] for s in symbols}
    if not symbols:
        return out
    with engine.connect() as conn:
        for f in layer1_findings(conn, symbols, as_of, params):
            if f.rule in STRUCTURAL_RULES:
                label = (
                    "acquired / delisted"
                    if f.rule == "acquired_or_delisted"
                    else ("listing-deficiency notice (8-K 3.01)")
                )
                out[f.symbol].append(f"{label}: {f.form} {f.accession} filed {f.filed_at}")
        since = (as_of - timedelta(days=CHANGE_OF_CONTROL_DAYS)).isoformat()
        cleared = set(params.cleared_accessions)
        for r in conn.execute(
            select(
                filings.c.symbol,
                filings.c.accession,
                filings.c.form,
                filings.c.filed_at,
                filings.c["items"],
            )
            .where(
                filings.c.symbol.in_(list(symbols)),
                filings.c.form.in_(EIGHT_K),
                filings.c.filed_at >= since,
                filings.c.filed_at <= as_of.isoformat(),
            )
            .order_by(filings.c.symbol, filings.c.filed_at, filings.c.accession)
        ):
            items = set(json.loads(r._mapping["items"]) or [])
            if not {"2.01", "5.01"} <= items or r.accession in cleared:
                continue
            usable, _eid = _event_for(conn, r.accession)
            if usable:
                out[r.symbol].append(
                    f"acquired: 8-K Items 2.01 + 5.01 {r.accession} filed {r.filed_at}"
                )
    return out
