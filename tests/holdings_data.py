"""Synthetic fixtures for the M5 holdings/targets/rebalance tests. Every symbol other than the
real benchmarks the code keys on (QTUM/QQQ) is made up, and every number is generated."""

from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import Engine, insert

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    event_classifications,
    event_tickers,
    events,
    filings,
    prices_daily,
    strategy_metrics,
    strategy_runs,
    strategy_weights,
)
from aether.db.types import utcnow_iso
from tests.conftest import seed_tickers
from tests.portfolio_data import UNIVERSE, random_closes, weekdays

PROFILE_NAMES = ("safe", "medium", "aggressive")


def seed_universe(engine: Engine) -> None:
    seed_tickers(engine, UNIVERSE)


def seed_prices(
    engine: Engine,
    n: int = 200,
    start: date = date(2026, 1, 2),
    overrides: dict[str, list[float]] | None = None,
) -> list[str]:
    """Calm synthetic closes for every universe symbol over n weekdays. Returns the dates."""
    seed_universe(engine)
    days = weekdays(n, start)
    rows = []
    for i, (sym, _) in enumerate(UNIVERSE):
        closes = (overrides or {}).get(sym) or random_closes(n, seed=500 + i, vol=0.01)
        rows += [
            {
                "symbol": sym,
                "d": d,
                "o": c,
                "h": c,
                "l": c,
                "c": c,
                "volume": 1000,
                "provider": "synthetic",
                "fetched_at": utcnow_iso(),
            }
            for d, c in zip(days, closes, strict=True)
        ]
    with write_tx(engine) as conn:
        upsert(conn, prices_daily, rows, key_cols=["symbol", "d"])
    return days


def fake_run(
    engine: Engine, as_of: str, weights: dict[str, dict[str, float] | None], tag: str = ""
) -> int:
    """A strategy run with chosen recommended weights per profile (None = no qualifying
    strategy). Profiles not given reuse the first given weights."""
    first = next(w for w in weights.values() if w is not None)
    summary = {"as_of": as_of, "profiles": {}}
    rows, wrows = [], []
    for p in PROFILE_NAMES:
        w = weights.get(p, first)
        sid = None if w is None else f"{p}:core_equal:q{tag or '50'}"
        summary["profiles"][p] = {"strategy_id": sid, "reason": "synthetic"}
        if w is None:
            continue
        rows.append(
            {
                "strategy_id": sid,
                "kind": "candidate",
                "profile": p,
                "family": "core_equal",
                "qtum_weight": w.get("QTUM", 0.0),
                "metrics": "{}",
                "qualifies": "{}",
            }
        )
        wrows += [{"strategy_id": sid, "symbol": s, "weight": v} for s, v in w.items()]
    digest = hashlib.sha256(f"{as_of}{tag}{json.dumps(weights, sort_keys=True)}".encode()).digest()
    with write_tx(engine) as conn:
        run_id: int = conn.execute(
            insert(strategy_runs)
            .values(
                as_of=as_of,
                input_hash=digest,
                config="{}",
                summary=json.dumps(summary),
                created_at=utcnow_iso(),
            )
            .returning(strategy_runs.c.id)
        ).scalar_one()
        if rows:
            conn.execute(insert(strategy_metrics), [{**r, "run_id": run_id} for r in rows])
        if wrows:
            conn.execute(insert(strategy_weights), [{**r, "run_id": run_id} for r in wrows])
    return run_id


_event_seq = [0]


def add_event(
    engine: Engine,
    symbol: str,
    published_at: str,
    materiality: int = 5,
    cls: str = "RISK",
    category: str = "going_concern",
    quarantined: bool = False,
) -> int:
    _event_seq[0] += 1
    n = _event_seq[0]
    url = f"https://example.test/event/{n}"
    with write_tx(engine) as conn:
        eid: int = conn.execute(
            insert(events)
            .values(
                url_hash=hashlib.sha256(url.encode()).digest(),
                title=f"Synthetic {category} event {n} for {symbol}",
                url=url,
                source_domain="example.test",
                trust_tier="T1",
                published_at=published_at,
                origin="manual",
                quarantined=int(quarantined),
                created_at=utcnow_iso(),
            )
            .returning(events.c.id)
        ).scalar_one()
        conn.execute(insert(event_tickers).values(event_id=eid, symbol=symbol))
        conn.execute(
            insert(event_classifications).values(
                event_id=eid,
                **{"class": cls},
                category=category,
                materiality_raw=materiality,
                materiality=materiality,
                direction=-1,
                confidence=1.0,
                rule_id="synthetic_rule",
                created_at=utcnow_iso(),
            )
        )
    return eid


_acc_seq = [0]


def add_filing(
    engine: Engine,
    symbol: str,
    form: str,
    filed_at: str,
    *,
    items: list[str] | None = None,
    parsed: dict | None = None,
    event: tuple[str, str, int, str] | None = None,  # (class, category, materiality, rule_id)
    quarantined: bool = False,
) -> tuple[str, int | None]:
    """A synthetic filing (example.test URL, made-up accession) plus, optionally, its rule
    event. Returns (accession, event id)."""
    _acc_seq[0] += 1
    acc = f"0009999999-26-{_acc_seq[0]:06d}"
    url = f"https://example.test/filing/{acc}"
    with write_tx(engine) as conn:
        conn.execute(
            insert(filings).values(
                accession=acc,
                symbol=symbol,
                cik="0009999999",
                form=form,
                filed_at=filed_at,
                items=json.dumps(items or []),
                url=url,
                parsed=None if parsed is None else json.dumps(parsed),
                fetched_at=utcnow_iso(),
            )
        )
        eid = None
        if event is not None:
            cls, category, materiality, rule_id = event
            eid = conn.execute(
                insert(events)
                .values(
                    url_hash=hashlib.sha256(url.encode()).digest(),
                    title=f"{symbol} {form} (synthetic)",
                    url=url,
                    source_domain="example.test",
                    trust_tier="T1",
                    published_at=f"{filed_at}T21:00:00Z",
                    origin="edgar",
                    accession=acc,
                    quarantined=int(quarantined),
                    created_at=utcnow_iso(),
                )
                .returning(events.c.id)
            ).scalar_one()
            conn.execute(insert(event_tickers).values(event_id=eid, symbol=symbol))
            conn.execute(
                insert(event_classifications).values(
                    event_id=eid,
                    **{"class": cls},
                    category=category,
                    materiality_raw=materiality,
                    materiality=materiality,
                    direction=-1,
                    confidence=1.0,
                    rule_id=rule_id,
                    created_at=utcnow_iso(),
                )
            )
    return acc, eid
