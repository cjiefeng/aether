"""Stance hysteresis and cooldown (spec §6.2, M10): enforced in code, pure functions."""

from __future__ import annotations

from datetime import date

from aether.config import load_weights
from aether.synthesize.hysteresis import CitedEvent, Previous, band_of, decide, score_trigger
from tests.conftest import CONFIG_DIR

P = load_weights(CONFIG_DIR).conclusions
PREV = Previous(id=1, stance="HOLD", created_at="2026-09-06T00:30:00Z", last_change="2026-08-01")
TODAY = date(2026, 10, 4)


def ev(
    i: int,
    m: int = 4,
    cls: str = "SIGNAL",
    tier: str = "T2",
    at: str = "2026-09-20T12:00:00Z",
    q: bool = False,
) -> CitedEvent:
    return CitedEvent(i, cls, m, tier, at, q)


def run(
    proposed: str,
    cited: list[int] = (),
    events: dict[int, CitedEvent] | None = None,  # type: ignore[assignment]
    totals: list[float | None] | None = None,
    prev: Previous | None = PREV,
    today: date = TODAY,
    kind: str = "ticker",
):  # type: ignore[no-untyped-def]
    return decide(
        kind=kind,
        proposed=proposed,
        previous=prev,
        cited_event_ids=list(cited),
        events=events or {},
        totals=totals or [],
        as_of=today,
        p=P,
    )


def test_first_conclusion_and_same_stance_are_accepted() -> None:
    d = run("AVOID", prev=None)
    assert (d.stance, d.held) == ("AVOID", False)
    d = run("HOLD")
    assert (d.stance, d.held) == ("HOLD", False)


def test_flip_without_a_qualifying_trigger_is_held() -> None:
    d = run("ACCUMULATE")
    assert (d.stance, d.held) == ("HOLD", True)
    assert d.reason and d.reason.startswith("no qualifying trigger")
    # A cited event that's too small, too old or quarantined doesn't qualify either.
    for e in (ev(5, m=3), ev(5, at="2026-09-01T00:00:00Z"), ev(5, q=True)):
        assert run("ACCUMULATE", [5], {5: e}).held


def test_material_event_since_the_last_conclusion_accepts_the_flip() -> None:
    d = run("ACCUMULATE", [7], {7: ev(7)})
    assert (d.stance, d.held, d.trigger) == ("ACCUMULATE", False, "material_event")


def test_score_threshold_needs_margin_for_consecutive_days() -> None:
    # HOLD band is [-20, 20); up-flip needs >= 20 + 20 for 5 consecutive days.
    assert band_of(25, P) == "ACCUMULATE" and band_of(-60, P) == "AVOID"
    ok = [41.0, 45.0, 40.0, 50.0, 42.0]
    assert score_trigger("HOLD", "ACCUMULATE", ok, P)
    assert not score_trigger("HOLD", "ACCUMULATE", [10.0, *ok[1:]], P)  # one day short
    assert not score_trigger("HOLD", "ACCUMULATE", ok[1:], P)  # only four days
    assert not score_trigger("HOLD", "ACCUMULATE", [30.0] * 5, P)  # crossed, not by the margin
    assert score_trigger("HOLD", "TRIM", [-41.0] * 5, P)
    assert not score_trigger("HOLD", "TRIM", [-41.0, None, -41.0, -41.0, -41.0], P)
    d = run("ACCUMULATE", totals=ok)
    assert (d.stance, d.held, d.trigger) == ("ACCUMULATE", False, "score_threshold")


def test_cooldown_blocks_a_qualified_flip() -> None:
    recent = Previous(1, "HOLD", "2026-09-27T00:30:00Z", last_change="2026-09-27")
    d = run("ACCUMULATE", [7], {7: ev(7, at="2026-09-30T00:00:00Z")}, prev=recent)
    assert (d.stance, d.held) == ("HOLD", True)
    assert d.reason == "cooldown until 2026-10-11"
    # After the cooldown the same flip goes through.
    d = run(
        "ACCUMULATE",
        [7],
        {7: ev(7, at="2026-09-30T00:00:00Z")},
        prev=recent,
        today=date(2026, 10, 11),
    )
    assert (d.stance, d.held) == ("ACCUMULATE", False)


def test_t1_risk_event_bypasses_the_cooldown_on_a_downgrade_only() -> None:
    recent = Previous(1, "HOLD", "2026-09-27T00:30:00Z", last_change="2026-09-27")
    risk = {9: ev(9, m=5, cls="RISK", tier="T1", at="2026-10-01T00:00:00Z")}
    d = run("AVOID", [9], risk, prev=recent)
    assert (d.stance, d.held) == ("AVOID", False)
    assert d.reason and "bypassed the cooldown" in d.reason
    # A T2 RISK event doesn't bypass; neither does a T1 RISK event cited for an upgrade.
    t2 = {9: ev(9, m=5, cls="RISK", tier="T2", at="2026-10-01T00:00:00Z")}
    assert run("AVOID", [9], t2, prev=recent).held
    assert run("ACCUMULATE", [9], risk, prev=recent).held


def test_theme_tilt_has_no_score_trigger() -> None:
    prev = Previous(1, "NEUTRAL", "2026-09-06T00:30:00Z", "2026-08-01")
    d = run("PURE_PLAYS", totals=[90.0] * 5, prev=prev, kind="theme")
    assert d.held and "score" not in (d.reason or "")
    d = run("PURE_PLAYS", [7], {7: ev(7)}, prev=prev, kind="theme")
    assert (d.stance, d.held) == ("PURE_PLAYS", False)
