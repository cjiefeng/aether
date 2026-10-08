"""M11 + M13 acceptance: escalation (spec §5.2.5).

M13 rules: RISK only; materiality 5, or 4 in a severe category (delisting only for a deficiency
notice or the common stock, dilution only at >= 10% of FD shares). Verification only without a T1
source. Caps: 2 a day, 72 h per ticker, the escalation sub-budget. Results only on change.
M11 behaviour kept: alert + re-synthesis within 5 minutes; quarantined / injection-suspected
events never escalate; the verification call carries the event title only inside an untrusted
block. Synthetic data only (ACME, DEMO, example.test); the real SDK talks to a fake transport."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select

from aether.alerts.candidates import AlertCandidate, collect
from aether.alerts.dispatch import enqueue
from aether.config import (
    EscalationParams,
    load_alerts_config,
    load_llm_config,
    load_rubric,
    load_sources,
    load_weights,
)
from aether.db.engine import write_tx
from aether.db.models import alerts, conclusions, escalations, llm_calls, research_runs
from aether.db.types import utcnow_iso
from aether.escalate.run import EscalationDeps, changes, run_escalations
from aether.escalate.select import Recent, candidates, offering_size, refusal
from aether.facts import load_facts
from aether.llm.client import BudgetExceeded, EscalationBudgetExceeded
from aether.llm.pricing import ESCALATION_SYNTH_PURPOSE, sgt_day_start
from aether.research.runner import VerifyTarget, run_verify
from aether.synthesize.run import SynthDeps, synthesize_one
from tests.conclusions_data import (
    SYNTH_MODEL,
    add_conclusion,
    payload,
    seed_closes,
    synth_message,
)
from tests.conftest import CONFIG_DIR, seed_tickers
from tests.fundamentals_data import COMMON, WARRANTS, add_facts, fact
from tests.holdings_data import add_event, add_filing
from tests.llm_fakes import (
    FakeApi,
    classify_message,
    classify_payload,
    llm_config,
    llm_settings,
    make_client,
    research_message,
)
from tests.llm_fakes import search_result as sr

P = load_llm_config(CONFIG_DIR).escalation
W = load_weights(CONFIG_DIR)
ALERTS = load_alerts_config(CONFIG_DIR)
RISK_FLAGS = load_rubric(CONFIG_DIR).risk_flags
DAY = timedelta(days=1)
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
        "max_per_day": 2,
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


def test_caps_daily_cooldown_and_budget() -> None:
    now = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)  # 11:00 SGT
    cool = timedelta(hours=P.cooldown_hours)
    assert P.cooldown_hours == 72
    day = sgt_day_start(now)
    two = [Recent(s, iso(day + timedelta(minutes=10 * i))) for i, s in enumerate(SYMS[:2])]
    assert refusal("OMGA", now, two[:1], max_per_day=2, cooldown=cool) is None
    # The 3rd escalation in a day is refused.
    assert refusal("OMGA", now, two, max_per_day=2, cooldown=cool) == "daily_cap"
    # Escalations before SGT midnight don't count toward today's cap.
    yesterday = [Recent(s, iso(day - timedelta(minutes=5))) for s in SYMS[:2]]
    assert refusal("OMGA", now, yesterday, max_per_day=2, cooldown=cool) is None
    # A 2nd on the same ticker within 72 h is refused; after 72 h it's allowed.
    recent = [Recent("ACME", iso(now - timedelta(hours=71, minutes=59)))]
    assert refusal("ACME", now, recent, max_per_day=2, cooldown=cool) == "ticker_cooldown"
    assert refusal("DEMO", now, recent, max_per_day=2, cooldown=cool) is None
    old = [Recent("ACME", iso(now - timedelta(hours=72, minutes=1)))]
    assert refusal("ACME", now, old, max_per_day=2, cooldown=cool) is None
    assert refusal("ACME", now, [], max_per_day=0, cooldown=cool) == "daily_cap"
    # The sub-budget refuses last.
    assert refusal("ACME", now, [], max_per_day=2, cooldown=cool, budget_left=False) == "budget"


def test_changes_decide_whether_the_result_is_sent() -> None:
    @dataclass
    class C:
        stance: str
        proposed_stance: str
        held: bool

    assert changes("HOLD", C("HOLD", "HOLD", False), [], []) == []
    assert changes("HOLD", C("AVOID", "AVOID", False), [], []) == ["stance HOLD → AVOID"]
    assert changes(None, C("HOLD", "HOLD", False), [], []) == ["stance none → HOLD"]
    assert changes("HOLD", C("HOLD", "AVOID", True), [], []) == ["flip to AVOID proposed, held"]
    assert changes("HOLD", None, [["stance", "HOLD", 1.0]], [["going_concern", 0.0]]) == [
        "overlay output changed"
    ]
    assert changes("HOLD", None, None, None) == []


# --------------------------------------------------------------------------- triggers


def fresh_day(now: datetime) -> str:
    return now.date().isoformat()


def fd_shares(engine: Engine, symbol: str, common: int, now: datetime) -> None:
    """A company-wide share count filed before the offering (no other FD components)."""
    end = (now - 60 * DAY).date().isoformat()
    add_facts(
        engine, [fact(symbol, COMMON, end, common, filed=(now - 30 * DAY).date().isoformat())]
    )


def test_signal_and_noise_never_escalate(engine: Engine) -> None:
    now = datetime.now(UTC)
    at = iso(now - timedelta(hours=1))
    add_event(engine, "ACME", at, 5, "SIGNAL", "contract_with_value")
    add_event(engine, "DEMO", at, 5, "NOISE", "physical_qubit_count")
    add_event(engine, "EXMP", at, 4, "SIGNAL", "verified_advantage")
    assert candidates(engine, P, SYMS, now, lookback_days=3) == []
    assert run_escalations(engine, stub_deps(engine), now=now).rows_written == 0


def test_routine_filings_alert_but_dont_escalate(engine: Engine) -> None:
    now = datetime.now(UTC)
    d = fresh_day(now)
    fd_shares(engine, "ACME", 100_000_000, now)
    small = {"offering": {"shares": 5_000_000, "prefunded": 0}, "atm": None}
    events = [
        add_filing(engine, "ACME", "S-3", d, event=("RISK", "dilution", 3, "s3"))[1],
        add_filing(engine, "ACME", "424B5", d, parsed=small, event=("RISK", "dilution", 4, "b5"))[
            1
        ],
        # Size not parseable: alerts, never escalates.
        add_filing(
            engine, "ACME", "424B5", d, parsed={"atm": None}, event=("RISK", "dilution", 4, "b5")
        )[1],
        add_filing(
            engine,
            "ACME",
            "25-NSE",
            d,
            parsed={"security": "Warrants", "covers_common": False},
            event=("RISK", "delisting_or_compliance", 4, "f25"),
        )[1],
        add_filing(
            engine,
            "ACME",
            "8-K",
            d,
            items=["3.02"],
            parsed={},
            event=("RISK", "dilution", 3, "k302"),
        )[1],
        # Voluntary transfer under Item 3.01, and a 5.01 change in control: alert only.
        add_filing(
            engine,
            "ACME",
            "8-K",
            d,
            items=["3.01"],
            parsed={"listing_notice": "transfer"},
            event=("RISK", "delisting_or_compliance", 4, "k301"),
        )[1],
        add_filing(
            engine,
            "ACME",
            "8-K",
            d,
            items=["5.01"],
            parsed={},
            event=("RISK", "delisting_or_compliance", 4, "k501"),
        )[1],
    ]
    alerted = {
        c.event_id for c in collect(engine, ALERTS, RISK_FLAGS, now) if c.kind == "risk_event"
    }
    assert set(events) <= alerted
    assert candidates(engine, P, SYMS, now, lookback_days=3) == []


def test_going_concern_large_offering_and_deficiency_escalate(engine: Engine) -> None:
    now = datetime.now(UTC)
    d = fresh_day(now)
    fd_shares(engine, "ACME", 90_000_000, now)
    add_facts(  # warrants count toward FD shares: 90M + 10M = 100M
        engine,
        [
            fact(
                "ACME",
                WARRANTS,
                (now - 60 * DAY).date().isoformat(),
                10_000_000,
                filed=(now - 30 * DAY).date().isoformat(),
            )
        ],
    )
    gc = add_event(engine, "DEMO", iso(now - timedelta(hours=2)), 5, "RISK", "going_concern")
    big = {"offering": {"shares": 12_000_000, "prefunded": 0}, "atm": None}
    _, b5 = add_filing(engine, "ACME", "424B5", d, parsed=big, event=("RISK", "dilution", 4, "b5"))
    _, k301 = add_filing(
        engine,
        "EXMP",
        "8-K",
        d,
        items=["3.01"],
        parsed={"listing_notice": "deficiency"},
        event=("RISK", "delisting_or_compliance", 4, "k301"),
    )
    got = {c.event_id: c for c in candidates(engine, P, SYMS, now, lookback_days=3)}
    assert set(got) == {gc, b5, k301}
    assert got[gc].trigger == "materiality_5"
    assert got[b5].trigger == "severe_category"
    assert got[b5].reason.startswith("offering ≈ 12.0% of 100,000,000 fully diluted shares")
    assert got[k301].reason == "listing-deficiency notice (8-K 3.01)"
    assert all(c.has_t1 for c in got.values())


def test_atm_size_uses_the_last_close(engine: Engine) -> None:
    now = datetime.now(UTC)
    d = fresh_day(now)
    fd_shares(engine, "ACME", 100_000_000, now)
    seed_closes(engine, {"ACME": [((now - 2 * DAY).date().isoformat(), 10.0)]})
    acc, _ = add_filing(
        engine,
        "ACME",
        "424B5",
        d,
        parsed={"atm": {"amount": "150000000"}, "offering": None},
        event=("RISK", "dilution", 4, "b5"),
    )
    with engine.connect() as conn:
        size = offering_size(conn, "ACME", acc)
    assert size is not None and size.shares == 15_000_000 and size.pct == pytest.approx(0.15)
    assert "ATM up to $150,000,000" in size.basis


def test_candidates_skip_quarantined_injection_old_and_untracked(engine: Engine) -> None:
    now = datetime.now(UTC)
    fresh = iso(now - timedelta(hours=1))
    ok = add_event(engine, "ACME", fresh, 5)
    add_event(engine, "TEST", fresh, 5, quarantined=True)
    add_event(engine, "ZETA", fresh, 5, injection_suspected=True)
    add_event(engine, "OMGA", iso(now - timedelta(days=4)), 5)  # outside the 3-day lookback
    add_event(engine, "QQQ", fresh, 5)  # no conclusions for benchmarks
    add_event(engine, "DEMO", fresh, 3, "RISK", "going_concern")  # below 4
    got = candidates(engine, P, SYMS, now, lookback_days=3)
    assert [(c.event_id, c.symbol, c.trigger) for c in got] == [(ok, "ACME", "materiality_5")]


# --------------------------------------------------------------------------- caps end to end


def test_third_escalation_in_a_day_is_refused(engine: Engine) -> None:
    now = datetime.now(UTC)
    start = max(sgt_day_start(now), now - timedelta(hours=2))
    ids = [
        add_event(engine, s, iso(start + timedelta(seconds=i)), 5) for i, s in enumerate(SYMS[:3])
    ]
    result = run_escalations(engine, stub_deps(engine), now=now)
    rows = esc_rows(engine)
    assert [r["event_id"] for r in rows] == ids
    assert [r["status"] for r in rows] == ["failed"] * 2 + ["refused"]
    assert rows[2]["refusal"] == "daily_cap"
    assert result.rows_written == 3 and "daily_cap" in (result.warning or "")
    kinds = alert_kinds(engine)
    assert kinds.count("escalation") == 2 and kinds.count("escalation_result") == 2
    assert run_escalations(engine, stub_deps(engine), now=now).rows_written == 0
    assert len(esc_rows(engine)) == 3 and len(alert_kinds(engine)) == 4


def test_second_event_on_a_ticker_within_72_hours_is_refused(engine: Engine) -> None:
    now = datetime.now(UTC)
    a = add_event(engine, "ACME", iso(now - timedelta(hours=2)), 5)
    run_escalations(engine, stub_deps(engine), now=now)
    b = add_event(engine, "ACME", iso(now - timedelta(hours=1)), 5)
    run_escalations(engine, stub_deps(engine), now=now + timedelta(hours=48))
    later = now + timedelta(hours=72, minutes=1)
    c = add_event(engine, "ACME", iso(later - timedelta(minutes=30)), 5)
    run_escalations(engine, stub_deps(engine), now=later)
    rows = {r["event_id"]: r for r in esc_rows(engine)}
    assert rows[a]["status"] == "failed" and rows[a]["refusal"] is None
    assert (rows[b]["status"], rows[b]["refusal"]) == ("refused", "ticker_cooldown")
    assert rows[c]["refusal"] is None


def test_disabled_llm_still_alerts_and_records_why(engine: Engine) -> None:
    add_event(engine, "ACME", iso(datetime.now(UTC) - timedelta(hours=1)), 5)
    run_escalations(engine, stub_deps(engine))
    (r,) = esc_rows(engine)
    d = json.loads(r["detail"])
    assert d["verify"] == {"status": "skipped", "reason": "T1 source"}
    assert d["synthesis"]["status"] == "skipped"
    with engine.connect() as conn:
        texts = list(conn.execute(select(alerts.c.text).order_by(alerts.c.id)).scalars())
    assert texts[0].startswith("RISK · ACME · going concern (materiality 5/5)")
    assert "Escalated (1/2 today): materiality 5." in texts[0]
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
        purpose=ESCALATION_SYNTH_PURPOSE,
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
        budget_usd=s.escalation_daily_budget_usd,
    )


def avoid_message(eid: int, today: str) -> dict[str, Any]:
    body = payload(
        "ACME",
        today,
        stance="AVOID",
        thesis=[{"point": "Synthetic adverse event.", "evidence_ids": [f"E{eid}"]}],
        stance_change_justification={"trigger": "material_event", "evidence_ids": [f"E{eid}"]},
    )
    return synth_message(body)


def purposes(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(select(llm_calls.c.purpose).order_by(llm_calls.c.id)).scalars())


def test_t1_escalation_resynthesizes_without_verification_within_five_minutes(
    engine: Engine, migrated_db: Path
) -> None:
    now = datetime.now(UTC)
    eid = add_event(engine, "ACME", iso(now - timedelta(minutes=20)), 5, "RISK", "going_concern")
    api = FakeApi(messages=[avoid_message(eid, now.date().isoformat())])
    result = run_escalations(engine, llm_deps(engine, migrated_db, api))
    assert result.rows_written == 1 and result.warning is None

    (r,) = esc_rows(engine)
    assert r["status"] == "done" and r["trigger"] == "materiality_5"
    assert r["research_run_id"] is None
    d = json.loads(r["detail"])
    assert d["verify"] == {"status": "skipped", "reason": "T1 source"}
    assert d["changed"] == ["stance none → AVOID"]
    with engine.connect() as conn:
        concl = conn.execute(select(conclusions)).one()
        assert conn.execute(select(research_runs)).first() is None
        res = conn.execute(select(alerts).where(alerts.c.kind == "escalation_result")).one()
        ats = list(conn.execute(select(alerts.c.created_at).order_by(alerts.c.id)).scalars())
    assert concl.id == r["conclusion_id"] and concl.stance == "AVOID"
    assert purposes(engine) == ["synthesis_escalation"]  # no verification search
    assert "tools" not in api.bodies()[0]
    assert "Verification skipped: T1 source." in res.text
    t0 = now - timedelta(seconds=5)
    for at in (*ats, concl.created_at, r["finished_at"]):
        assert datetime.fromisoformat(at) - t0 < timedelta(minutes=5)
    assert run_escalations(engine, llm_deps(engine, migrated_db, api)).rows_written == 0


def test_t2_only_escalation_verifies_and_resynthesizes(engine: Engine, migrated_db: Path) -> None:
    now = datetime.now(UTC)
    eid = add_event(
        engine,
        "ACME",
        iso(now - timedelta(minutes=20)),
        4,
        "RISK",
        "short_report",
        trust_tier="T2",
    )
    found = research_message(
        [sr("https://example.test/acme-followup", "Synthetic ACME follow-up report", "2 hours ago")]
    )
    api = FakeApi(messages=[found, avoid_message(eid, now.date().isoformat())])
    run_escalations(engine, llm_deps(engine, migrated_db, api))

    (r,) = esc_rows(engine)
    assert r["status"] == "done" and r["trigger"] == "severe_category"
    d = json.loads(r["detail"])
    assert d["verify"] == {
        "status": "done",
        "research_run_id": r["research_run_id"],
        "events_new": 1,
    }
    assert purposes(engine) == ["research_verify", "synthesis_escalation"]
    verify_req, synth_req = api.bodies()
    assert [t["type"] for t in verify_req["tools"]] == ["web_search_20260209"]
    assert verify_req["tools"][0]["max_uses"] == P.verify_max_uses
    prompt = verify_req["messages"][0]["content"]
    title = "Synthetic short_report event"
    assert f'<untrusted_document id="event-{eid}">' in prompt
    before, _, rest = prompt.partition("<untrusted_document")
    assert title not in before and title in rest.partition("</untrusted_document>")[0]
    assert "tools" not in synth_req


def test_unchanged_resynthesis_stays_on_the_dashboard(engine: Engine, migrated_db: Path) -> None:
    now = datetime.now(UTC)
    today = now.date().isoformat()
    add_conclusion(engine, "ACME", (now - 10 * DAY).date().isoformat(), "AVOID")
    eid = add_event(engine, "ACME", iso(now - timedelta(minutes=20)), 5, "RISK", "going_concern")
    api = FakeApi(messages=[avoid_message(eid, today)])
    deps = llm_deps(engine, migrated_db, api)
    sent: list[AlertCandidate] = []
    deps = replace(deps, notify=lambda cands: sent.extend(cands))
    run_escalations(engine, deps)
    (r,) = esc_rows(engine)
    assert r["status"] == "done" and json.loads(r["detail"])["changed"] == []
    result = [c for c in sent if c.kind == "escalation_result"]
    assert [c.delivery for c in result] == ["dashboard_only"]
    assert result[0].text.splitlines()[1] == "No change."


def test_stance_change_result_is_sent(engine: Engine, migrated_db: Path) -> None:
    now = datetime.now(UTC)
    add_conclusion(engine, "ACME", (now - 40 * DAY).date().isoformat(), "HOLD")
    eid = add_event(engine, "ACME", iso(now - timedelta(minutes=20)), 5, "RISK", "going_concern")
    api = FakeApi(messages=[avoid_message(eid, now.date().isoformat())])
    sent: list[AlertCandidate] = []
    deps = replace(llm_deps(engine, migrated_db, api), notify=lambda cands: sent.extend(cands))
    run_escalations(engine, deps)
    (result,) = [c for c in sent if c.kind == "escalation_result"]
    assert result.delivery == "immediate"
    assert "Changed: stance HOLD → AVOID." in result.text


def test_overlay_change_alone_sends_the_result(engine: Engine) -> None:
    add_event(engine, "ACME", iso(datetime.now(UTC) - timedelta(hours=1)), 5)
    states = iter([[["stance", "HOLD", 1.0]], [["going_concern", 0.0]]])
    sent: list[AlertCandidate] = []
    deps = stub_deps(
        engine, notify=lambda cands: sent.extend(cands), overlay_state=lambda _s: next(states)
    )
    run_escalations(engine, deps)
    (result,) = [c for c in sent if c.kind == "escalation_result"]
    assert result.delivery == "immediate" and "overlay output changed" in result.text


def spend(engine: Engine, purpose: str, usd: str) -> None:
    with write_tx(engine) as conn:
        conn.execute(
            insert(llm_calls).values(
                purpose=purpose,
                model="claude-opus-5-5",
                cost_micros=Decimal(usd),
                created_at=utcnow_iso(),
                status="ok",
            )
        )


def test_escalation_sub_budget_refuses_while_classification_still_runs(
    engine: Engine, migrated_db: Path
) -> None:
    spend(engine, "research_verify", "0.60")
    spend(engine, ESCALATION_SYNTH_PURPOSE, "0.90")  # $1.50: the sub-budget is used up
    add_event(engine, "ACME", iso(datetime.now(UTC) - timedelta(hours=1)), 5)
    api = FakeApi(messages=[classify_message(classify_payload())])
    run_escalations(engine, llm_deps(engine, migrated_db, api))
    (r,) = esc_rows(engine)
    assert (r["status"], r["refusal"]) == ("refused", "budget")
    assert alert_kinds(engine) == []  # a refused escalation sends nothing

    llm = make_client(engine, llm_settings(migrated_db), api)
    # Escalation calls are refused by the sub-budget...
    with pytest.raises(EscalationBudgetExceeded):
        llm.complete(
            purpose=ESCALATION_SYNTH_PURPOSE,
            model=SYNTH_MODEL,
            system="s",
            messages=[{"role": "user", "content": "u"}],
            max_tokens=1024,
        )
    # ...while classification (inside the daily budget, $1.50 of $5 spent) still runs.
    llm.complete(
        purpose="classify",
        model="claude-sonnet-5-5",
        system="s",
        messages=[{"role": "user", "content": "u"}],
        max_tokens=1024,
    )
    assert len(api.requests) == 1


def test_sub_budget_stop_mid_escalation_ends_refused(engine: Engine, migrated_db: Path) -> None:
    spend(engine, "research_verify", "1.45")  # under the cap at claim, not enough to re-synthesize
    add_event(engine, "ACME", iso(datetime.now(UTC) - timedelta(hours=1)), 5)
    api = FakeApi()
    run_escalations(engine, llm_deps(engine, migrated_db, api))
    (r,) = esc_rows(engine)
    assert (r["status"], r["refusal"]) == ("refused", "budget")
    assert json.loads(r["detail"])["synthesis"]["status"] == "refused"
    assert api.requests == []


def test_daily_budget_refusal_records_failure(engine: Engine, migrated_db: Path) -> None:
    add_event(engine, "ACME", iso(datetime.now(UTC) - timedelta(hours=1)), 5, trust_tier="T2")
    api = FakeApi()
    run_escalations(engine, llm_deps(engine, migrated_db, api, daily_llm_budget_usd="0"))
    (r,) = esc_rows(engine)
    assert r["status"] == "failed"
    d = json.loads(r["detail"])
    assert d["verify"]["status"] == "failed" and "budget" in d["verify"]["error"]
    assert "budget" in (d["synthesis"]["error"] or "")
    assert api.requests == []
    with engine.connect() as conn:
        assert conn.execute(select(research_runs.c.status)).scalar_one() == "budget_refused"
    assert issubclass(EscalationBudgetExceeded, BudgetExceeded)


def test_scheduler_registers_escalations_and_digest_jobs(
    rw_engine: Engine, migrated_db: Path
) -> None:
    from aether.jobs import ESCALATION_MINUTES, build_scheduler
    from tests.conftest import make_settings

    sched = build_scheduler(rw_engine, make_settings(migrated_db))
    job = sched.get_job("escalations")
    assert job is not None and job.trigger.interval == timedelta(minutes=ESCALATION_MINUTES)
    drill = {f.name: str(f) for f in sched.get_job("restore_drill").trigger.fields}
    assert (drill["day_of_week"], drill["hour"], drill["minute"]) == ("sun", "4", "30")
    digest = {f.name: str(f) for f in sched.get_job("alert_digest").trigger.fields}
    assert (digest["hour"], digest["minute"]) == ("8", "0")


def test_config_has_escalation_block() -> None:
    assert (
        EscalationParams(
            min_materiality=5,
            severe_min_materiality=4,
            severe_categories=(
                "going_concern",
                "short_report",
                "guidance_cut",
                "delisting_or_compliance",
                "dilution",
            ),
            large_dilution_pct=0.10,
            cooldown_hours=72,
            verify_max_uses=3,
            verify_window_days=7,
        )
        == P
    )
    assert ALERTS.immediate_min_materiality == 4 and ALERTS.digest_time == "08:00"
    with pytest.raises(ValueError):
        EscalationParams(**{**P.model_dump(), "severe_min_materiality": 5, "min_materiality": 4})


def test_settings_defaults(migrated_db: Path) -> None:
    from tests.conftest import make_settings

    s = make_settings(migrated_db)
    assert s.max_escalations_per_day == 2 and s.escalation_daily_budget_usd == Decimal("1.50")
