"""Synthetic fixtures for the M10 conclusion tests. Symbols other than the real benchmarks the
code keys on (QTUM/QQQ) are made up (ACME, DEMO, ...); every number and text is generated."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, insert

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import conclusion_outcomes, conclusions, prices_daily
from aether.db.types import utcnow_iso
from tests.llm_fakes import message

SYNTH_MODEL = "claude-opus-5-5"


def add_conclusion(
    engine: Engine,
    symbol: str | None,
    as_of: str,
    stance: str,
    *,
    held: bool = False,
    proposed: str | None = None,
    confidence: float = 0.6,
    created_at: str | None = None,
    prev_id: int | None = None,
) -> int:
    kind = "ticker" if symbol else "theme"
    payload = {"one_line_verdict": f"Synthetic verdict for {symbol or 'theme'}.", "thesis": []}
    with write_tx(engine) as conn:
        cid: int = conn.execute(
            insert(conclusions)
            .values(
                kind=kind,
                symbol=symbol,
                as_of=as_of,
                created_at=created_at or f"{as_of}T00:30:00Z",
                stance=stance,
                proposed_stance=proposed or stance,
                held=int(held),
                hold_reason="synthetic" if held else None,
                confidence=confidence,
                horizon="12m",
                payload=json.dumps(payload),
                evidence="{}",
                model=SYNTH_MODEL,
                prompt_version="synth-test",
                input_hash=hashlib.sha256(f"{symbol}{as_of}{stance}".encode()).digest(),
                cost_micros=Decimal(0),
                prev_id=prev_id,
            )
            .returning(conclusions.c.id)
        ).scalar_one()
    return cid


def add_outcome(engine: Engine, cid: int, horizon: str, hit: int, hold: int, mom: int) -> None:
    with write_tx(engine) as conn:
        conn.execute(
            insert(conclusion_outcomes).values(
                conclusion_id=cid,
                horizon=horizon,
                benchmark="QTUM",
                start_d="2025-01-02",
                end_d="2025-07-02",
                excess_return=0.1 if hit else -0.1,
                hit=hit,
                hold_hit=hold,
                momentum_stance="AVOID",
                momentum_hit=mom,
                status="complete",
                computed_at=utcnow_iso(),
            )
        )


def seed_closes(engine: Engine, series: dict[str, list[tuple[str, float]]]) -> None:
    rows = [
        {
            "symbol": s,
            "d": d,
            "o": c,
            "h": c,
            "l": c,
            "c": c,
            "volume": 1000,
            "provider": "synthetic",
            "fetched_at": utcnow_iso(),
        }
        for s, pts in series.items()
        for d, c in pts
    ]
    with write_tx(engine) as conn:
        upsert(conn, prices_daily, rows, key_cols=["symbol", "d"])


def weekdays_between(start: date, end: date) -> list[str]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def payload(symbol: str, as_of: str, **over: Any) -> dict[str, Any]:
    """A valid ticker conclusion for the synthetic request (override any field)."""
    p: dict[str, Any] = {
        "ticker": symbol,
        "as_of": as_of,
        "stance": "HOLD",
        "confidence": 0.55,
        "horizon": "12m",
        "one_line_verdict": "Synthetic verdict.",
        "thesis": [
            {"point": "Synthetic point citing the scorecard.", "evidence_ids": ["K:" + symbol]}
        ],
        "bear_case": [{"point": "Synthetic bear point.", "evidence_ids": []}],
        "what_would_change_my_mind": ["A synthetic development."],
        "key_dates": [],
        "stance_change_justification": {"trigger": "none", "evidence_ids": []},
        "injection_suspected": False,
    }
    p.update(over)
    return p


def theme_payload(as_of: str, **over: Any) -> dict[str, Any]:
    p = payload("X", as_of, **{k: v for k, v in over.items() if k != "as_of"})
    p.pop("ticker")
    p.pop("stance")
    p.setdefault("tilt", "NEUTRAL")
    p.setdefault("sleeve_note", "Synthetic: the basket explains a share of QTUM's moves.")
    p["thesis"] = over.get(
        "thesis", [{"point": "Synthetic theme point.", "evidence_ids": ["X:theme"]}]
    )
    return p


def synth_message(body: dict[str, Any] | str, stop_reason: str = "end_turn") -> dict[str, Any]:
    text = body if isinstance(body, str) else json.dumps(body)
    m = message(
        [{"type": "thinking", "thinking": "", "signature": "c2ln"}, {"type": "text", "text": text}],
        model=SYNTH_MODEL,
    )
    m["stop_reason"] = stop_reason
    return m
