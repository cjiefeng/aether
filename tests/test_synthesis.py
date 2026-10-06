"""M10 acceptance: synthesis with no tools, the citation validator (retry once) and hysteresis.

- no unvalidated or quarantined citation reaches the DB;
- a proposed flip without a qualifying trigger is stored as `held`;
- holdings never appear in a prompt; no tools are ever sent.
Synthetic data only (ACME, DEMO, example.test); the real SDK talks to a fake transport."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from aether.config import load_weights
from aether.db.models import conclusion_failures, conclusions, llm_calls
from aether.facts import load_facts
from aether.portfolio.holdings import HoldingsUpdate, PositionIn, apply_holdings_update
from aether.synthesize.run import PURPOSE, SynthDeps, run_conclusions, synthesize_one
from tests.conclusions_data import (
    SYNTH_MODEL,
    add_conclusion,
    payload,
    synth_message,
    theme_payload,
)
from tests.conftest import CONFIG_DIR, seed_tickers
from tests.holdings_data import add_event
from tests.llm_fakes import FakeApi, llm_config, llm_settings, make_client

AS_OF = date(2026, 10, 4)
DAY = AS_OF.isoformat()
W = load_weights(CONFIG_DIR)


@pytest.fixture
def engine(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, [("ACME", "pure_play"), ("DEMO", "pure_play"), ("QTUM", "etf")])
    return rw_engine


def deps(engine: Engine, db: Path, api: FakeApi, **settings: object) -> SynthDeps:
    s = llm_settings(db, **settings)
    return SynthDeps(
        llm=make_client(engine, s, api),
        llm_cfg=llm_config(),
        model=SYNTH_MODEL,
        conclusions=W.conclusions,
        track=W.track_record,
        facts=load_facts(CONFIG_DIR),
    )


def rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(r._mapping) for r in conn.execute(select(conclusions).order_by(conclusions.c.id))
        ]


def failures(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(conclusion_failures))]


def test_valid_answer_is_stored_with_no_tools_and_evidence(
    engine: Engine, migrated_db: Path
) -> None:
    eid = add_event(engine, "ACME", "2026-09-20T12:00:00Z", 4, "SIGNAL", "contract_with_value")
    body = payload(
        "ACME",
        DAY,
        stance="ACCUMULATE",
        thesis=[{"point": "Synthetic contract.", "evidence_ids": [f"E{eid}", "K:ACME"]}],
    )
    api = FakeApi(messages=[synth_message(body)])
    out = synthesize_one(engine, deps(engine, migrated_db, api), "ticker", "ACME", AS_OF)
    assert out["status"] == "ok"
    (r,) = rows(engine)
    assert (r["stance"], r["held"], r["proposed_stance"], r["kind"]) == (
        "ACCUMULATE",
        0,
        "ACCUMULATE",
        "ticker",
    )
    assert r["model"] == SYNTH_MODEL and r["prompt_version"].startswith("synth-v1-")
    ev = json.loads(r["evidence"])
    assert set(ev) == {f"E{eid}", "K:ACME"} and ev[f"E{eid}"]["url"].startswith(
        "https://example.test/"
    )
    (req,) = api.bodies()
    assert "tools" not in req and req["model"] == SYNTH_MODEL
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["effort"] == "high"
    user = req["messages"][0]["content"]
    # Event text goes inside the untrusted block; the S1 notice is in the system prompt.
    assert '<untrusted_document id="events-ACME">' in user and f"E{eid}" in user
    assert "untrusted" in req["system"][0]["text"]
    with engine.connect() as conn:
        purposes = conn.execute(select(llm_calls.c.purpose)).scalars().all()
    assert purposes == [PURPOSE]


def test_unknown_citation_twice_stores_nothing_and_logs_the_failure(
    engine: Engine, migrated_db: Path
) -> None:
    bad = payload("ACME", DAY, thesis=[{"point": "x", "evidence_ids": ["E999999"]}])
    api = FakeApi(messages=[synth_message(bad), synth_message(bad)])
    out = synthesize_one(engine, deps(engine, migrated_db, api), "ticker", "ACME", AS_OF)
    assert out["status"] == "failed"
    assert rows(engine) == []
    (f,) = failures(engine)
    assert f["attempts"] == 2 and "unknown or quarantined evidence ids: E999999" in f["error"]
    # The retry tells the model what our validator rejected (no untrusted text).
    second = api.bodies()[1]["messages"][0]["content"]
    assert "rejected by the validator: unknown or quarantined evidence ids: E999999" in second


def test_quarantined_event_is_not_citable_and_a_valid_retry_is_stored(
    engine: Engine, migrated_db: Path
) -> None:
    q = add_event(engine, "ACME", "2026-09-21T12:00:00Z", 5, "RISK", "dilution", quarantined=True)
    first = payload("ACME", DAY, thesis=[{"point": "x", "evidence_ids": [f"E{q}"]}])
    api = FakeApi(messages=[synth_message(first), synth_message(payload("ACME", DAY))])
    d = deps(engine, migrated_db, api)
    out = synthesize_one(engine, d, "ticker", "ACME", AS_OF)
    assert out["status"] == "ok"
    first_prompt = api.bodies()[0]["messages"][0]["content"]
    assert f"E{q}" not in first_prompt  # quarantined events never enter the context
    (r,) = rows(engine)
    assert f"E{q}" not in r["payload"] and f"E{q}" not in r["evidence"]


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"thesis": [{"point": "no evidence", "evidence_ids": []}]}, "cites no evidence"),
        ({"injection_suspected": True}, "injection_suspected"),
        ({"ticker": "DEMO"}, "ticker must be ACME"),
        ({"as_of": "2026-01-01"}, "as_of must be"),
        ({"confidence": 1.5}, "schema: confidence"),
        (
            {"key_dates": [{"date": "2026-11-01", "event": "x", "catalyst_id": 42}]},
            "unknown catalyst",
        ),
    ],
)
def test_invalid_answers_are_rejected_not_repaired(
    engine: Engine, migrated_db: Path, over: dict, error: str
) -> None:
    bad = {**payload("ACME", DAY), **over}
    api = FakeApi(messages=[synth_message(bad), synth_message(bad)])
    synthesize_one(engine, deps(engine, migrated_db, api), "ticker", "ACME", AS_OF)
    assert rows(engine) == []
    assert error in failures(engine)[0]["error"]


def test_refusal_and_truncation_are_failures(engine: Engine, migrated_db: Path) -> None:
    api = FakeApi(
        messages=[
            synth_message("", stop_reason="refusal"),
            synth_message('{"ticker": "AC', stop_reason="max_tokens"),
        ]
    )
    synthesize_one(engine, deps(engine, migrated_db, api), "ticker", "ACME", AS_OF)
    assert rows(engine) == []
    assert "truncated" in failures(engine)[0]["error"]


def test_flip_without_trigger_is_stored_as_held(engine: Engine, migrated_db: Path) -> None:
    prev = add_conclusion(engine, "ACME", "2026-08-30", "HOLD")
    body = payload(
        "ACME",
        DAY,
        stance="AVOID",
        stance_change_justification={"trigger": "material_event", "evidence_ids": []},
    )
    api = FakeApi(messages=[synth_message(body)])
    out = synthesize_one(engine, deps(engine, migrated_db, api), "ticker", "ACME", AS_OF)
    assert out["status"] == "held"
    r = rows(engine)[-1]
    assert (r["stance"], r["proposed_stance"], r["held"], r["prev_id"]) == (
        "HOLD",
        "AVOID",
        1,
        prev,
    )
    assert r["hold_reason"].startswith("no qualifying trigger")


def test_flip_with_a_cited_material_event_is_accepted(engine: Engine, migrated_db: Path) -> None:
    add_conclusion(engine, "ACME", "2026-08-30", "HOLD")
    eid = add_event(engine, "ACME", "2026-09-25T12:00:00Z", 5, "RISK", "dilution")
    body = payload(
        "ACME",
        DAY,
        stance="AVOID",
        thesis=[{"point": "Synthetic dilution.", "evidence_ids": [f"E{eid}"]}],
        stance_change_justification={"trigger": "material_event", "evidence_ids": [f"E{eid}"]},
    )
    api = FakeApi(messages=[synth_message(body)])
    synthesize_one(engine, deps(engine, migrated_db, api), "ticker", "ACME", AS_OF)
    r = rows(engine)[-1]
    assert (r["stance"], r["held"]) == ("AVOID", 0)
    assert json.loads(r["payload"])["hysteresis"]["trigger"] == "material_event"


def test_qtum_and_theme_must_cite_the_theme_decomposition(
    engine: Engine, migrated_db: Path
) -> None:
    no_theme = payload("QTUM", DAY)
    with_theme = payload(
        "QTUM", DAY, thesis=[{"point": "Basket explains part.", "evidence_ids": ["X:theme"]}]
    )
    api = FakeApi(messages=[synth_message(no_theme), synth_message(with_theme)])
    synthesize_one(engine, deps(engine, migrated_db, api), "ticker", "QTUM", AS_OF)
    r = rows(engine)[-1]
    assert r["symbol"] == "QTUM" and json.loads(r["payload"])["label"] == "quantum-sleeve view"
    assert "X:theme" in api.bodies()[1]["messages"][0]["content"]


def test_holdings_never_reach_a_prompt(engine: Engine, migrated_db: Path) -> None:
    apply_holdings_update(
        engine,
        HoldingsUpdate(
            positions=(PositionIn(symbol="ACME", shares=Decimal("1234.5678")),),
            cash=Decimal("98765.43"),
        ),
        None,
    )
    api = FakeApi(messages=[synth_message(payload("ACME", DAY))])
    synthesize_one(engine, deps(engine, migrated_db, api), "ticker", "ACME", AS_OF)
    sent = json.dumps(api.bodies())
    assert "1234.5678" not in sent and "98765" not in sent and "$CASH" not in sent


def test_run_order_theme_last_and_budget_stop(engine: Engine, migrated_db: Path) -> None:
    msgs = [
        synth_message(payload("ACME", DAY)),
        synth_message(payload("DEMO", DAY)),
        synth_message(payload("QTUM", DAY, thesis=[{"point": "p", "evidence_ids": ["X:theme"]}])),
        synth_message(theme_payload(DAY, tilt="PURE_PLAYS")),
    ]
    api = FakeApi(messages=msgs)
    res = run_conclusions(engine, deps(engine, migrated_db, api), AS_OF)
    assert res.rows_written == 4 and res.warning is None
    got = [(r["kind"], r["symbol"], r["stance"]) for r in rows(engine)]
    assert got == [
        ("ticker", "ACME", "HOLD"),
        ("ticker", "DEMO", "HOLD"),
        ("ticker", "QTUM", "HOLD"),
        ("theme", None, "PURE_PLAYS"),
    ]
    theme_prompt = api.bodies()[3]["messages"][0]["content"]
    assert "T:ACME" in theme_prompt and "quantum-sleeve view" in theme_prompt
    # A tiny daily budget refuses before any request is sent.
    api2 = FakeApi(messages=[])
    res = run_conclusions(
        engine, deps(engine, migrated_db, api2, daily_llm_budget_usd="0.001"), AS_OF
    )
    assert res.rows_written == 0 and res.warning and "budget" in res.warning
    assert api2.requests == []
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(conclusions)).scalar_one() == 4
