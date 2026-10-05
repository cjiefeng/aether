"""The daily strategies job: input hash, idempotence, byte-identical output, selection, pruning."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text, update

from aether.config import load_strategies
from aether.db import migrate
from aether.db.engine import ensure_db_file, make_rw_engine, write_tx
from aether.db.models import (
    prices_daily,
    strategy_curves,
    strategy_metrics,
    strategy_runs,
    strategy_weights,
)
from aether.portfolio import job
from aether.portfolio.job import compute_run, input_hash, load_inputs, run_strategies
from tests.conftest import CONFIG_DIR
from tests.portfolio_data import SLEEVE, panel, seed_panel

CONFIG = load_strategies(CONFIG_DIR)


def _count(engine: Engine, table: object) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())  # type: ignore[arg-type]


def _dump(engine: Engine) -> list[tuple[object, ...]]:
    """Every stored output row except the run id and timestamp."""
    with engine.connect() as conn:
        out: list[tuple[object, ...]] = list(
            conn.execute(text("SELECT as_of, input_hash, config, summary FROM strategy_runs")).all()
        )
        for q in (
            "SELECT strategy_id, kind, profile, family, qtum_weight, metrics, qualifies "
            "FROM strategy_metrics ORDER BY strategy_id",
            "SELECT strategy_id, symbol, weight FROM strategy_weights ORDER BY 1, 2",
            "SELECT series_id, points FROM strategy_curves ORDER BY 1",
        ):
            out += list(conn.execute(text(q)).all())
    return out


@pytest.fixture
def seeded(rw_engine: Engine) -> Engine:
    seed_panel(rw_engine, panel(late={"FAKE": 170}))
    return rw_engine


def test_run_stores_everything(seeded: Engine) -> None:
    res = run_strategies(seeded, CONFIG)
    assert res.rows_written > 0
    assert _count(seeded, strategy_runs) == 1
    # 4 families x (2 + 3 + 3) grid points + 3 benchmarks
    assert _count(seeded, strategy_metrics) == 4 * 3 + 3  # 4 families x 3 fixed-QTUM profiles
    with seeded.connect() as conn:
        summary = json.loads(conn.execute(select(strategy_runs.c.summary)).scalar_one())
        sums = conn.execute(
            select(strategy_weights.c.strategy_id, func.sum(strategy_weights.c.weight)).group_by(
                strategy_weights.c.strategy_id
            )
        ).all()
    assert len(sums) == 12 and all(abs(s - 1) < 1e-9 for _, s in sums)
    assert summary["universe"] == ["QTUM", *SLEEVE]
    assert any(c.startswith("FAKE has 90 sessions of history") for c in summary["caveats"])
    for p in ("safe", "medium", "aggressive"):
        assert summary["profiles"][p]["strategy_id"] is not None


def test_rerun_on_same_inputs_is_a_no_op(seeded: Engine) -> None:
    run_strategies(seeded, CONFIG)
    before = _dump(seeded)
    assert run_strategies(seeded, CONFIG).rows_written == 0
    assert _count(seeded, strategy_runs) == 1
    assert _dump(seeded) == before


def test_same_inputs_give_byte_identical_output(seeded: Engine, tmp_path: Path) -> None:
    other_path = tmp_path / "other" / "aether.db"
    ensure_db_file(other_path)
    migrate.upgrade(other_path)
    other = make_rw_engine(other_path)
    try:
        seed_panel(other, panel(late={"FAKE": 170}))
        run_strategies(seeded, CONFIG)
        run_strategies(other, CONFIG)
        assert _dump(seeded) == _dump(other)
    finally:
        other.dispose()
    a, b = load_inputs(seeded), load_inputs(seeded)
    assert input_hash(a, CONFIG) == input_hash(b, CONFIG)


def test_changed_price_or_config_gives_a_new_run(seeded: Engine) -> None:
    run_strategies(seeded, CONFIG)
    h1 = input_hash(load_inputs(seeded), CONFIG)
    with write_tx(seeded) as conn:
        conn.execute(
            update(prices_daily)
            .where(prices_daily.c.symbol == "ACME", prices_daily.c.d == "2025-06-02")
            .values(c=prices_daily.c.c * 1.01)
        )
    h2 = input_hash(load_inputs(seeded), CONFIG)
    assert h1 != h2
    run_strategies(seeded, CONFIG)
    assert _count(seeded, strategy_runs) == 2
    cfg = CONFIG.model_copy(
        update={"backtest": CONFIG.backtest.model_copy(update={"cost_bps": 20.0})}
    )
    assert input_hash(load_inputs(seeded), cfg) != h2


def test_dividends_raise_total_return_by_expected_amount(rw_engine: Engine) -> None:
    closes = panel()
    ex_day, ex_close = closes["QTUM"][200]
    prev_close = closes["QTUM"][199][1]
    seed_panel(rw_engine, closes)
    plain = compute_run(load_inputs(rw_engine), CONFIG)
    seed_panel(rw_engine, closes, {"QTUM": {ex_day: Decimal("0.75")}})
    with_div = compute_run(load_inputs(rw_engine), CONFIG)
    assert plain is not None and with_div is not None

    def qtum_total(out: job.RunOutput) -> float:
        row = next(r for r in out.metrics if r["strategy_id"] == "QTUM")
        return float(row["metrics"]["total_return"])

    # Reinvesting the dividend multiplies that day's growth by (c + div) / c.
    expected = (1 + qtum_total(plain)) * (ex_close + 0.75) / ex_close - 1
    assert qtum_total(with_div) == pytest.approx(expected, rel=1e-12)
    assert prev_close > 0


def test_profile_limit_breaking_candidates_never_selected(seeded: Engine) -> None:
    safe = CONFIG.profiles["safe"].model_copy(update={"vol_limit_x": 0.01})
    cfg = CONFIG.model_copy(update={"profiles": {**CONFIG.profiles, "safe": safe}})
    out = compute_run(load_inputs(seeded), cfg)
    assert out is not None
    p = out.summary["profiles"]["safe"]
    assert p["strategy_id"] is None
    assert p["reason"].startswith("No qualifying strategy")
    for prof in ("medium", "aggressive"):
        sid = out.summary["profiles"][prof]["strategy_id"]
        row = next(r for r in out.metrics if r["strategy_id"] == sid)
        assert row["qualifies"]["ok"] is True


def test_short_history_skips_without_a_run(rw_engine: Engine) -> None:
    closes = {s: v[:100] for s, v in panel().items()}
    seed_panel(rw_engine, closes)
    res = run_strategies(rw_engine, CONFIG)
    assert res.warning and "needs more than" in res.warning
    assert _count(rw_engine, strategy_runs) == 0


def test_old_curves_are_pruned(seeded: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job, "KEEP_CURVE_RUNS", 1)
    run_strategies(seeded, CONFIG)
    with write_tx(seeded) as conn:
        conn.execute(
            update(prices_daily)
            .where(prices_daily.c.symbol == "QQQ")
            .values(c=prices_daily.c.c * 1.0001)
        )
    run_strategies(seeded, CONFIG)
    with seeded.connect() as conn:
        runs = set(conn.execute(select(strategy_curves.c.run_id)).scalars())
        latest = conn.execute(select(func.max(strategy_runs.c.id))).scalar_one()
    assert runs == {latest}
    assert _count(seeded, strategy_metrics) == 2 * 15  # metrics and weights are kept


def test_scheduler_registers_portfolio_job(rw_engine: Engine, migrated_db: Path) -> None:
    from aether.jobs import build_scheduler
    from tests.conftest import make_settings

    sched = build_scheduler(rw_engine, make_settings(migrated_db))
    job_ = sched.get_job("portfolio")
    assert job_ is not None
    fields = {f.name: str(f) for f in job_.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("7", "10")  # 07:10 SGT, after prices
    assert str(job_.trigger.timezone) == "Asia/Singapore"


def test_each_profile_uses_its_fixed_qtum_weight(rw_engine: Engine) -> None:
    """M5 acceptance: each profile's QTUM weight equals its configured fixed value."""
    from sqlalchemy import select

    seed_panel(rw_engine, panel())
    run_strategies(rw_engine, CONFIG)
    with rw_engine.connect() as conn:
        rows = conn.execute(
            select(strategy_metrics.c.profile, strategy_metrics.c.qtum_weight).where(
                strategy_metrics.c.kind == "candidate"
            )
        ).all()
    for profile, q in rows:
        assert q == CONFIG.profiles[profile].qtum_weight
    assert {p: CONFIG.profiles[p].qtum_weight for p in CONFIG.profiles} == {
        "safe": 0.75,
        "medium": 0.45,
        "aggressive": 0.15,
    }
