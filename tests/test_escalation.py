"""M11 acceptance: escalation (spec §5.2.5).

- a synthetic high-materiality T1 event → an alert and a re-synthesis within 5 minutes;
- the 6th escalation in an SGT day is refused; one per ticker per 6 hours;
- quarantined / injection-suspected events never escalate;
- the verification call carries the event title only inside an untrusted block.
Synthetic data only (ACME, DEMO, example.test); the real SDK talks to a fake transport."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from aether.alerts.candidates import AlertCandidate
from aether.alerts.dispatch import enqueue
from aether.config import EscalationParams, load_llm_config, load_sources, load_weights
from aether.db.models import alerts, conclusions, escalations, llm_calls, research_runs
from aether.escalate.run import EscalationDeps, run_escalations
from aether.escalate.select import Recent, candidates, refusal, trigger_for
from aether.facts import load_facts
from aether.llm.pricing import sgt_day_start
from aether.research.runner import VerifyTarget, run_verify
from aether.synthesize.run import SynthDeps, synthesize_one
from tests.conclusions_data import SYNTH_MODEL, payload, synth_message
from tests.conftest import CONFIG_DIR, seed_tickers
from tests.holdings_data import add_event
from tests.llm_fakes import FakeApi, llm_config, llm_settings, make_client, research_message
from tests.llm_fakes import search_result as sr

P = load_llm_config(CONFIG_DIR).escalation
W = load_weights(CONFIG_DIR)
SYMS = ("ACME", "DEMO", "EXMP", "TEST", "ZETA", "OMGA", "QTUM")


@pytest.fixture
def engine(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, [(s, "etf" if s == "QTUM" else "pure_play") for s in SYMS])
    seed_tickers(rw_engine, [("QQQ", "benchmark")])
    return rw_engine


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def notifier(engine: Engine) -> Any:
    def notify(cands: Sequence[AlertCandidate]) -> None:
        enqueue(engine, cands, telegram=False, now=datetime.now(UTC))

    return notify


def stub_deps(engine: Engine, **over: Any) -> EscalationDeps:
    """No LLM: escalations still alert, then fail with the disabled reason."""
    values: dict[str, Any] = {
        "params": P,
        "max_per_day": 5,
        "lookback_days": 3,
        "symbols": frozenset(SYMS),
        "names": {},
        "notify": notifier(engine),
        "verify": None,
        "classify": lambda: None,
        "resynthesize": None,
        "disabled_reason": "ANTHROPIC_API_KEY is not set",
    }
    values.update(over)
    return EscalationDeps(**values)


def esc_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r._mapping) for r in conn.execute(select(escalations).order_by(escalations.c.id))
        ]


def alert_kinds(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(select(alerts.c.kind).order_by(alerts.c.id)).scalars())


# --------------------------------------------------------------------------- pure rules


def test_triggers() -> None:
    assert trigger_for("SIGNAL", 4, "T3", P) == "materiality"
    assert trigger_for("RISK", 5, "T2", P) == "materiality"
    assert trigger_for("RISK", 3, "T1", P) == "t1_risk"
    assert trigger_for("RISK", 3, "T2", P) is None
    assert trigger_for("SIGNAL", 3, "T1", P) is None
    assert trigger_for("NOISE", 2, "T1", P) is None


def test_daily_cap_counts_sgt_day_and_cooldown_is_per_ticker() -> None:
    now = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)  # 11:00 SGT
    six_h = timedelta(hours=6)
    day = sgt_day_start(now)
    five = [Recent(s, iso(day + timedelta(minutes=10 * i))) for i, s in enumerate(SYMS[:5])]
    assert refusal("OMGA", now, five[:4], max_per_day=5, cooldown=six_h) is None
    assert refusal("OMGA", now, five, max_per_day=5, cooldown=six_h) == "daily_cap"
    # Escalations before SGT midnight don't count toward today's cap.
    yesterday = [Recent(s, iso(day - timedelta(minutes=5))) for s in SYMS[:5]]
    assert refusal("OMGA", now, yesterday, max_per_day=5, cooldown=six_h) is None
    # Same ticker within 6h → cooldown; after 6h it's allowed.
    recent = [Recent("ACME", iso(now - timedelta(hours=5, minutes=59)))]
    assert refusal("ACME", now, recent, max_per_day=5, cooldown=six_h) == "ticker_cooldown"
    assert refusal("DEMO", now, recent, max_per_day=5, cooldown=six_h) is None
    old = [Recent("ACME", iso(now - timedelta(hours=6, minutes=1)))]
    assert refusal("ACME", now, old, max_per_day=5, cooldown=six_h) is None
    assert refusal("ACME", now, [], max_per_day=0, cooldown=six_h) == "daily_cap"


def test_candidates_skip_quarantined_injection_old_and_untracked(engine: Engine) -> None:
    now = datetime.now(UTC)
    fresh = iso(now - timedelta(hours=1))
    ok_t1 = add_event(engine, "ACME", fresh, 3, "RISK", "dilution", trust_tier="T1")
    ok_m4 = add_event(engine, "DEMO", fresh, 4, "SIGNAL", "contract_with_value", trust_tier="T3")
    add_event(engine, "EXMP", fresh, 3, "RISK", "dilution", trust_tier="T2")  # below both
    add_event(engine, "TEST", fresh, 5, quarantined=True)
    add_event(engine, "ZETA", fresh, 5, injection_suspected=True)
    add_event(engine, "OMGA", iso(now - timedelta(days=4)), 5)  # outside the 3-day lookback
    add_event(engine, "QQQ", fresh, 5)  # no conclusions for benchmarks
    got = candidates(engine, P, SYMS, now, lookback_days=3)
    assert [(c.event_id, c.symbol, c.trigger) for c in got] == [
        (ok_t1, "ACME", "t1_risk"),
        (ok_m4, "DEMO", "materiality"),
    ]


# --------------------------------------------------------------------------- caps end to end


def test_sixth_escalation_in_a_day_is_refused(engine: Engine) -> None:
    now = datetime.now(UTC)
    start = max(sgt_day_start(now), now - timedelta(hours=2))
    ids = [
        add_event(engine, s, iso(start + timedelta(seconds=i)), 5) for i, s in enumerate(SYMS[:6])
    ]
    result = run_escalations(engine, stub_deps(engine), now=now)
    rows = esc_rows(engine)
    assert [r["event_id"] for r in rows] == ids
    assert [r["status"] for r in rows] == ["failed"] * 5 + ["refused"]
    assert rows[5]["refusal"] == "daily_cap"
    assert result.rows_written == 6 and "daily_cap" in (result.warning or "")
    # Five start + five result alerts; the refused one sends nothing.
    kinds = alert_kinds(engine)
    assert kinds.count("escalation") == 5 and kinds.count("escalation_result") == 5
    # Re-running claims nothing new, sends nothing new.
    assert run_escalations(engine, stub_deps(engine), now=now).rows_written == 0
    assert len(esc_rows(engine)) == 6 and len(alert_kinds(engine)) == 10


def test_second_event_on_a_ticker_within_six_hours_is_refused(engine: Engine) -> None:
    now = datetime.now(UTC)
    a = add_event(engine, "ACME", iso(now - timedelta(hours=2)), 5)
    run_escalations(engine, stub_deps(engine), now=now)
    b = add_event(engine, "ACME", iso(now - timedelta(hours=1)), 5)
    run_escalations(engine, stub_deps(engine), now=now + timedelta(hours=1))
    c = add_event(engine, "ACME", iso(now - timedelta(minutes=30)), 5)
    run_escalations(engine, stub_deps(engine), now=now + timedelta(hours=6, minutes=1))
    rows = {r["event_id"]: r for r in esc_rows(engine)}
    assert rows[a]["status"] == "failed" and rows[a]["refusal"] is None
    assert (rows[b]["status"], rows[b]["refusal"]) == ("refused", "ticker_cooldown")
    assert rows[c]["refusal"] is None


def test_disabled_llm_still_alerts_and_records_why(engine: Engine) -> None:
    add_event(engine, "ACME", iso(datetime.now(UTC) - timedelta(hours=1)), 5)
    run_escalations(engine, stub_deps(engine))
    (r,) = esc_rows(engine)
    d = json.loads(r["detail"])
    assert d["verify"]["status"] == "skipped" and d["synthesis"]["status"] == "skipped"
    with engine.connect() as conn:
        texts = list(conn.execute(select(alerts.c.text).order_by(alerts.c.id)).scalars())
    assert texts[0].startswith("Escalated (1/5 today) · ACME · RISK going concern")
    assert "Re-synthesis skipped: ANTHROPIC_API_KEY is not set" in texts[1]


# --------------------------------------------------------------------------- full flow (fake API)


def llm_deps(engine: Engine, db: Path, api: FakeApi, **settings: object) -> EscalationDeps:
    s = llm_settings(db, **settings)
    llm = make_client(engine, s, api)
    sdeps = SynthDeps(
        llm=llm,
        llm_cfg=llm_config(),
        model=SYNTH_MODEL,
        conclusions=W.conclusions,
        track=W.track_record,
        facts=load_facts(CONFIG_DIR),
    )
    classified: list[int] = []

    def verify(t: VerifyTarget) -> tuple[int, int]:
        return run_verify(
            engine,
            llm,
            llm_config(),
            load_sources(CONFIG_DIR),
            "claude-opus-5-5",
            t,
            max_uses=P.verify_max_uses,
            window_days=P.verify_window_days,
        )

    return stub_deps(
        engine,
        names={"ACME": "Acme Quantum"},
        verify=verify,
        classify=lambda: classified.append(1),
        resynthesize=lambda sym: synthesize_one(
            engine, sdeps, "ticker", sym, datetime.now(UTC).date()
        ),
        disabled_reason=None,
    )


def test_high_materiality_t1_event_alerts_and_resynthesizes_within_five_minutes(
    engine: Engine, migrated_db: Path
) -> None:
    now = datetime.now(UTC)
    eid = add_event(engine, "ACME", iso(now - timedelta(minutes=20)), 5, "RISK", "going_concern")
    today = now.date().isoformat()
    body = payload(
        "ACME",
        today,
        stance="AVOID",
        thesis=[{"point": "Synthetic going-concern doubt.", "evidence_ids": [f"E{eid}"]}],
        stance_change_justification={"trigger": "material_event", "evidence_ids": [f"E{eid}"]},
    )
    found = research_message(
        [sr("https://example.test/acme-followup", "Synthetic ACME follow-up report", "2 hours ago")]
    )
    api = FakeApi(messages=[found, synth_message(body)])
    result = run_escalations(engine, llm_deps(engine, migrated_db, api))
    assert result.rows_written == 1 and result.warning is None

    (r,) = esc_rows(engine)
    assert r["status"] == "done" and r["trigger"] == "materiality"
    d = json.loads(r["detail"])
    assert d["verify"] == {
        "status": "done",
        "research_run_id": r["research_run_id"],
        "events_new": 1,
    }
    with engine.connect() as conn:
        concl = conn.execute(select(conclusions)).one()
        run = conn.execute(select(research_runs)).one()
        ats = list(conn.execute(select(alerts.c.created_at).order_by(alerts.c.id)).scalars())
        purposes = list(
            conn.execute(select(llm_calls.c.purpose).order_by(llm_calls.c.id)).scalars()
        )
    assert concl.id == r["conclusion_id"] and concl.stance == "AVOID" and concl.symbol == "ACME"
    assert run.kind == "verify" and run.status == "done" and run.events_new == 1
    assert purposes == ["research_verify", "synthesis"]
    assert alert_kinds(engine) == ["escalation", "escalation_result"]
    # Alert and re-synthesis land within 5 minutes of the event being classified.
    t0 = now - timedelta(seconds=5)  # classification written by add_event just before `now`
    for at in (*ats, concl.created_at, r["finished_at"]):
        assert datetime.fromisoformat(at) - t0 < timedelta(minutes=5)

    # The verification call: web search only, and the title only inside an untrusted block.
    verify_req, synth_req = api.bodies()
    assert [t["type"] for t in verify_req["tools"]] == ["web_search_20260209"]
    assert verify_req["tools"][0]["max_uses"] == P.verify_max_uses
    prompt = verify_req["messages"][0]["content"]
    title = "Synthetic going_concern event"
    assert f'<untrusted_document id="event-{eid}">' in prompt
    before, _, rest = prompt.partition("<untrusted_document")
    assert title not in before and title in rest.partition("</untrusted_document>")[0]
    assert "tools" not in synth_req

    # Second pass: nothing new.
    assert run_escalations(engine, llm_deps(engine, migrated_db, api)).rows_written == 0


def test_budget_refusal_still_counts_and_records_failure(engine: Engine, migrated_db: Path) -> None:
    add_event(engine, "ACME", iso(datetime.now(UTC) - timedelta(hours=1)), 5)
    api = FakeApi()
    run_escalations(engine, llm_deps(engine, migrated_db, api, daily_llm_budget_usd="0"))
    (r,) = esc_rows(engine)
    assert r["status"] == "failed"
    d = json.loads(r["detail"])
    assert d["verify"]["status"] == "failed" and "budget" in d["verify"]["error"]
    assert "budget" in (d["synthesis"]["error"] or "")
    assert api.requests == []  # nothing left the process
    with engine.connect() as conn:
        assert conn.execute(select(research_runs.c.status)).scalar_one() == "budget_refused"


def test_scheduler_registers_escalations_job(rw_engine: Engine, migrated_db: Path) -> None:
    from aether.jobs import ESCALATION_MINUTES, build_scheduler
    from tests.conftest import make_settings

    sched = build_scheduler(rw_engine, make_settings(migrated_db))
    job = sched.get_job("escalations")
    assert job is not None and job.trigger.interval == timedelta(minutes=ESCALATION_MINUTES)
    drill = {f.name: str(f) for f in sched.get_job("restore_drill").trigger.fields}
    assert (drill["day_of_week"], drill["hour"], drill["minute"]) == ("sun", "4", "30")


def test_config_has_escalation_block() -> None:
    assert (
        EscalationParams(
            min_materiality=4,
            t1_risk_min_materiality=3,
            cooldown_hours=6,
            verify_max_uses=3,
            verify_window_days=7,
        )
        == P
    )
