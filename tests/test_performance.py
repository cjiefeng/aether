"""Issue #24: sleeve vs QTUM vs QQQ. Synthetic symbols (ACME, BETA) and synthetic prices only;
QTUM/QQQ appear because the code keys on them. Sessions come from the NYSE calendar (offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import pairwise

import numpy as np
import pytest

from aether import nyse
from aether.portfolio.metrics import compute_metrics
from aether.portfolio.performance import (
    SLEEVE,
    SLEEVE_HYPOTHETICAL,
    PerfInputs,
    Snapshot,
    compute_performance,
)

# 2026-02-23 (Mon) .. 2026-03-13 (Fri): 15 sessions, no holidays. US DST starts 2026-03-08,
# so closes are 21:00Z before that and 20:00Z after.
SESSIONS = [d.isoformat() for d in nyse.sessions(date(2026, 2, 23), date(2026, 3, 13))]
TODAY = date(2026, 3, 13)
CLOSES = [(d.isoformat(), nyse.close_utc(d)) for d in nyse.sessions(date(2026, 2, 1), TODAY)]
CLOSES_LONG = [(d.isoformat(), nyse.close_utc(d)) for d in nyse.sessions(date(2025, 2, 1), TODAY)]


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def flat(v: float = 100.0, moves: dict[str, float] | None = None) -> list[tuple[str, float]]:
    """Closes at `v`, with `moves` setting the close from that date on."""
    out, cur = [], v
    for d in SESSIONS:
        cur = (moves or {}).get(d, cur)
        out.append((d, cur))
    return out


def ramp(start: float, step: float) -> list[tuple[str, float]]:
    return [(d, start * (1 + step) ** i) for i, d in enumerate(SESSIONS)]


def snap(
    at: str, positions: dict[str, float], cash: float = 0.0, source: str = "manual"
) -> Snapshot:
    return Snapshot(ts(at), source, positions, cash)


def inputs(
    closes: dict,
    snapshots: list[Snapshot],
    current: Snapshot | None = None,
    dividends: dict | None = None,
) -> PerfInputs:
    base = {"QTUM": ramp(50, 0.002), "QQQ": ramp(400, 0.001)}
    return PerfInputs({**base, **closes}, dividends or {}, snapshots, current, CLOSES)


def series(body: dict, name: str) -> dict[str, float]:
    return {d: v for d, v in next(s for s in body["series"] if s["name"] == name)["data"]}


def test_actual_matches_hand_computed_twr_and_purchase_is_not_return() -> None:
    acme = [(d, 20 + 3 * np.sin(i)) for i, d in enumerate(SESSIONS)]
    beta = [(d, 50 + 2 * np.cos(i / 2)) for i, d in enumerate(SESSIONS)]
    s1 = snap("2026-02-24T22:30:00", {"ACME": 10})  # after Tue's close -> held from Tue close
    s2 = snap("2026-03-07T12:00:00", {"ACME": 10, "BETA": 5}, cash=100, source="tiger")  # Sat
    body = compute_performance(
        inputs({"ACME": acme, "BETA": beta}, [s1, s2]), "since", "actual", TODAY
    )
    px = {"ACME": dict(acme), "BETA": dict(beta)}
    days = SESSIONS[SESSIONS.index("2026-02-24") :]
    expect, level = {days[0]: 100.0}, 100.0
    for p, t in pairwise(days):
        held, cash = ({"ACME": 10}, 0.0) if p < "2026-03-06" else ({"ACME": 10, "BETA": 5}, 100.0)
        v = {s: n * px[s][p] for s, n in held.items()}
        r = sum(v[s] * (px[s][t] / px[s][p] - 1) for s in held) / (sum(v.values()) + cash)
        level *= 1 + r
        expect[t] = level
    got = series(body, SLEEVE)
    assert body["tracking_started"] == "2026-02-24" == body["start"]
    assert list(got) == days
    for d in days:
        assert got[d] == pytest.approx(expect[d], abs=1e-9)
    # Without the Saturday purchase the index is identical through Friday's close.
    only = series(
        compute_performance(inputs({"ACME": acme, "BETA": beta}, [s1]), "since", "actual", TODAY),
        SLEEVE,
    )
    assert all(only[d] == pytest.approx(got[d], abs=1e-12) for d in days if d <= "2026-03-06")
    assert body["markers"] == [
        {"date": "2026-02-24", "source": "manual"},
        {"date": "2026-03-06", "source": "tiger"},
    ]


@pytest.mark.parametrize(
    ("applied", "earns_monday", "earns_tuesday"),
    [
        ("2026-03-07T12:00:00", True, True),  # Saturday: held from Friday's close
        ("2026-03-09T15:00:00", True, True),  # during Monday: held from Friday's close
        ("2026-03-09T21:00:00", False, True),  # after Monday's 20:00Z close
        ("2026-03-10T15:00:00", False, True),  # during Tuesday: held from Monday's close
        ("2026-03-10T20:30:00", False, False),  # after Tuesday's close
    ],
)
def test_snapshot_timing(applied: str, earns_monday: bool, earns_tuesday: bool) -> None:
    acme = flat(100, {"2026-03-09": 110, "2026-03-10": 121})  # +10% Mon, +10% Tue
    cash_only = snap("2026-02-20T12:00:00", {}, cash=1000)
    stock = snap(applied, {"ACME": 10})
    body = compute_performance(inputs({"ACME": acme}, [cash_only, stock]), "since", "actual", TODAY)
    s = series(body, SLEEVE)
    mon = s["2026-03-09"] / s["2026-03-06"] - 1
    tue = s["2026-03-10"] / s["2026-03-09"] - 1
    assert mon == pytest.approx(0.10 if earns_monday else 0.0)
    assert tue == pytest.approx(0.10 if earns_tuesday else 0.0)


def test_dividends_raise_sleeve_and_benchmark_lines() -> None:
    divs = {"ACME": {"2026-03-04": 2.0}, "QTUM": {"2026-03-04": 1.0}}
    body = compute_performance(
        inputs(
            {"ACME": flat(100), "QTUM": flat(100)},
            [snap("2026-02-20T12:00:00", {"ACME": 10})],
            dividends=divs,
        ),
        "since",
        "actual",
        TODAY,
    )
    assert series(body, SLEEVE)["2026-03-13"] == pytest.approx(102.0)
    assert series(body, "QTUM")["2026-03-13"] == pytest.approx(101.0)


def test_cash_dilutes_return() -> None:
    acme = flat(100, {"2026-03-02": 110})
    body = compute_performance(
        inputs({"ACME": acme}, [snap("2026-02-20T12:00:00", {"ACME": 10}, cash=1000)]),
        "since",
        "actual",
        TODAY,
    )
    s = series(body, SLEEVE)
    assert s["2026-03-02"] / s["2026-02-27"] == pytest.approx(1.05)


def test_zero_value_period_is_flat_and_index_resumes() -> None:
    acme = ramp(100, 0.01)
    snaps = [
        snap("2026-02-20T12:00:00", {"ACME": 10}),
        snap("2026-03-02T22:00:00", {}, source="manual"),  # reset after Monday's close
        snap("2026-03-06T22:00:00", {"ACME": 5}),  # back after Friday's close
    ]
    s = series(compute_performance(inputs({"ACME": acme}, snaps), "since", "actual", TODAY), SLEEVE)
    assert s["2026-03-03"] == s["2026-03-06"] == pytest.approx(s["2026-03-02"])
    assert s["2026-03-09"] / s["2026-03-06"] == pytest.approx(1.01)
    assert s["2026-03-02"] / s["2026-02-27"] == pytest.approx(1.01)


def test_unchanged_resyncs_add_no_markers() -> None:
    same = [snap(f"2026-03-0{i}T12:00:00", {"ACME": 10}, source="tiger") for i in (2, 3, 4)]
    body = compute_performance(inputs({"ACME": flat()}, same), "since", "actual", TODAY)
    assert body["markers"] == [{"date": "2026-02-27", "source": "tiger"}]


def test_hypothetical_is_buy_and_hold_of_current_holdings() -> None:
    acme, beta = ramp(20, 0.01), ramp(50, -0.004)
    cur = snap("2026-03-13T12:00:00", {"ACME": 10, "BETA": 5}, cash=50)
    body = compute_performance(
        inputs({"ACME": acme, "BETA": beta}, [], current=cur), "1m", "current", TODAY
    )
    a, b = dict(acme), dict(beta)
    s = series(body, SLEEVE_HYPOTHETICAL)
    v = {d: 10 * a[d] + 5 * b[d] + 50 for d in s}
    first = next(iter(s))
    assert first == "2026-02-23" and body["markers"] == []
    for d in s:
        assert s[d] == pytest.approx(100 * v[d] / v[first], abs=1e-9)


def test_late_start_rebases_benchmarks_at_tracking_start() -> None:
    body = compute_performance(
        inputs({"ACME": ramp(20, 0.01)}, [snap("2026-03-04T22:00:00", {"ACME": 1})]),
        "1m",
        "actual",
        TODAY,
    )
    assert body["tracking_started"] == "2026-03-04" == body["start"]
    for s in body["series"]:
        assert s["data"][0] == ["2026-03-04", 100.0]


def test_gap_over_four_days_sets_stale_and_skips_days_for_every_line() -> None:
    beta = [(d, c) for d, c in ramp(50, 0.01) if not ("2026-03-02" <= d <= "2026-03-09")]
    body = compute_performance(
        inputs({"BETA": beta}, [snap("2026-02-20T12:00:00", {"BETA": 3})]), "since", "actual", TODAY
    )
    assert body["stale_since"] == "2026-02-27"
    # 03-02 and 03-03 are within 4 days of 02-27's close (carried forward); 03-04..03-09 aren't.
    for s in body["series"]:
        days = [d for d, _ in s["data"]]
        assert "2026-03-03" in days and "2026-03-10" in days
        assert not any("2026-03-04" <= d <= "2026-03-09" for d in days)
    s = series(body, SLEEVE)
    assert s["2026-03-10"] / s["2026-02-27"] == pytest.approx(1.01**7)


def test_stats_match_compute_metrics_and_annualize_only_for_a_year() -> None:
    acme = [(d, 20 + 3 * np.sin(i)) for i, d in enumerate(SESSIONS)]
    qqq = [(d, 400 + 9 * np.cos(i * 1.3)) for i, d in enumerate(SESSIONS)]  # beta needs var > 0
    body = compute_performance(
        inputs({"ACME": acme, "QQQ": qqq}, [snap("2026-02-20T12:00:00", {"ACME": 10})]),
        "1m",
        "actual",
        TODAY,
    )
    st = body["stats"]
    assert st["annualized"] is False
    days = [d for d, _ in body["series"][0]["data"]]

    def rets(name: str) -> np.ndarray:
        lv = np.array([v for _, v in next(s for s in body["series"] if s["name"] == name)["data"]])
        return lv[1:] / lv[:-1] - 1

    for line in st["lines"]:
        m = compute_metrics(rets(line["name"]), days[1:], {"QQQ": rets("QQQ")}, ann=252)
        assert line["total_return"] == pytest.approx(m["total_return"])
        assert line["volatility"] == pytest.approx(m["volatility"])
        assert line["max_drawdown"] == pytest.approx(m["max_drawdown"])
        assert line["annualized_return"] is None
        if line["name"] != "QQQ":
            assert line["beta_qqq"] == pytest.approx(m["beta_QQQ"])
    tr = {x["name"]: x["total_return"] for x in st["lines"]}
    assert st["excess_pp"]["QTUM"] == pytest.approx((tr[SLEEVE] - tr["QTUM"]) * 100)
    # A 1y range over three weeks of data isn't annualized either: the rule is the data's span.
    short = [snap("2026-02-20T12:00:00", {"ACME": 10})]
    one_y = compute_performance(inputs({"ACME": acme, "QQQ": qqq}, short), "1y", "actual", TODAY)
    assert one_y["stats"]["annualized"] is False


def test_a_year_of_history_is_annualized() -> None:
    days = [d.isoformat() for d in nyse.sessions(date(2025, 3, 3), TODAY)]
    wave = [(d, 100 + 5 * np.sin(i / 7)) for i, d in enumerate(days)]
    closes = {
        "ACME": wave,
        "QTUM": wave,
        "QQQ": [(d, 400 + 9 * np.cos(i)) for i, d in enumerate(days)],
    }
    body = compute_performance(
        PerfInputs(closes, {}, [snap("2025-03-01T12:00:00", {"ACME": 10})], None, CLOSES_LONG),
        "1y",
        "actual",
        TODAY,
    )
    st = body["stats"]
    assert st["annualized"] is True
    assert all(line["annualized_return"] is not None for line in st["lines"])


def test_not_enough_history() -> None:
    body = compute_performance(
        inputs({"ACME": flat()}, [snap("2026-03-13T22:00:00", {"ACME": 1})]), "1y", "actual", TODAY
    )
    assert body["enough"] is False and body["stats"] is None
