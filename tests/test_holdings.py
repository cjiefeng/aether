"""M5 acceptance: holdings edits go through `commands`, the worker applies them with history,
and the dashboard's command engine still can't write holdings directly. Holdings never reach
`llm_calls`."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, insert, select, text
from sqlalchemy.exc import DatabaseError
from starlette.testclient import TestClient

from aether.config import load_strategies
from aether.db.engine import make_command_engine
from aether.db.models import commands, holdings, holdings_history, llm_calls, rebalance_plans
from aether.jobs import process_commands
from aether.portfolio.holdings import (
    HoldingsUpdate,
    PositionIn,
    SettingsUpdate,
    apply_holdings_update,
    apply_settings_update,
    import_positions_yaml,
    load_holdings,
    load_settings,
)
from aether.portfolio.publish import run_rebalance
from aether.security.csrf import COOKIE_NAME
from tests.conftest import CONFIG_DIR
from tests.holdings_data import fake_run, seed_prices, seed_universe

CONFIG = load_strategies(CONFIG_DIR)
D = Decimal


def worker_handlers(engine: Engine) -> dict[str, Any]:
    """The same apply functions the worker's handlers call (jobs.build_scheduler)."""

    def upd(args: dict[str, Any]) -> dict[str, Any]:
        cid = args.pop("_command_id")
        return apply_holdings_update(engine, HoldingsUpdate.model_validate(args), cid)

    def settings(args: dict[str, Any]) -> dict[str, Any]:
        args.pop("_command_id")
        return apply_settings_update(engine, SettingsUpdate.model_validate(args))

    return {"update_holdings": upd, "update_portfolio_settings": settings}


def csrf(client: TestClient) -> dict[str, str]:
    client.get("/holdings")
    return {"X-CSRF-Token": client.cookies.get(COOKIE_NAME) or ""}


def test_holdings_edit_goes_through_commands(rw_engine: Engine, client: TestClient) -> None:
    seed_universe(rw_engine)
    r = client.post(
        "/commands/update-holdings",
        data={"shares_QTUM": "120", "cost_QTUM": "80.5", "shares_ACME": "7.25", "cash": "1,500"},
        headers=csrf(client),
    )
    assert r.status_code == 202, r.text
    assert load_holdings(rw_engine).empty  # nothing written by the dashboard
    with rw_engine.connect() as conn:
        cmd = conn.execute(select(commands).where(commands.c.kind == "update_holdings")).one()
    args = json.loads(cmd.args)
    assert args["cash"] == "1500"
    assert {p["symbol"] for p in args["positions"]} == {"QTUM", "ACME"}

    process_commands(rw_engine, handlers=worker_handlers(rw_engine))
    h = load_holdings(rw_engine)
    assert h.cash == D(1500)
    assert h.positions["QTUM"].shares == D(120) and h.positions["QTUM"].cost_basis == D("80.5")
    assert h.positions["ACME"].shares == D("7.25")
    with rw_engine.connect() as conn:
        hist = conn.execute(select(holdings_history)).one()
        status = conn.execute(select(commands.c.status).where(commands.c.id == cmd.id)).scalar()
    assert status == "done"
    assert hist.command_id == cmd.id and hist.source == "manual"
    assert json.loads(hist.before)["positions"] == {}
    assert json.loads(hist.after)["positions"]["ACME"]["shares"] == "7.25"


def test_command_engine_cannot_write_holdings(migrated_db: Path) -> None:
    engine = make_command_engine(migrated_db)
    try:
        for stmt in (
            insert(holdings).values(
                symbol="QTUM", shares_micros=D(1), source="manual", updated_at="x"
            ),
            text("UPDATE holdings SET shares_micros = 0"),
            text("DELETE FROM holdings"),
            text(
                "INSERT INTO holdings_history (source, before, after, applied_at) "
                "VALUES ('manual','{}','{}','x')"
            ),
            text("INSERT INTO portfolio_settings VALUES ('selected_profile','\"safe\"','x')"),
        ):
            with pytest.raises(DatabaseError) as exc, engine.begin() as conn:
                conn.execute(stmt)
            assert isinstance(exc.value.orig, sqlite3.DatabaseError)
            assert "not authorized" in str(exc.value.orig)
    finally:
        engine.dispose()


def test_invalid_holdings_rejected_with_400(rw_engine: Engine, client: TestClient) -> None:
    seed_universe(rw_engine)
    h = csrf(client)
    for data in (
        {"shares_QTUM": "-1", "cash": "0"},
        {"shares_QTUM": "abc", "cash": "0"},
        {"shares_QTUM": "1.1234567", "cash": "0"},
        {"cash": "-5"},
        {"shares_QTUM": "1e30", "cash": "0"},
    ):
        r = client.post("/commands/update-holdings", data=data, headers=h)
        assert r.status_code == 400, (data, r.text)
    with rw_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(commands)).scalar() == 0


def test_outside_universe_rejected_by_worker(rw_engine: Engine) -> None:
    seed_universe(rw_engine)
    upd = HoldingsUpdate(positions=(PositionIn(symbol="NOPE", shares=D(1)),), cash=D(0))
    with pytest.raises(ValueError, match="not in the strategy universe"):
        apply_holdings_update(rw_engine, upd)
    assert load_holdings(rw_engine).empty


def test_tiger_mode_applies_cash_only(rw_engine: Engine) -> None:
    seed_universe(rw_engine)
    apply_holdings_update(
        rw_engine, HoldingsUpdate(positions=(PositionIn(symbol="QTUM", shares=D(5)),), cash=D(1))
    )
    apply_settings_update(rw_engine, SettingsUpdate(holdings_source="tiger"))
    apply_holdings_update(
        rw_engine, HoldingsUpdate(positions=(PositionIn(symbol="ACME", shares=D(9)),), cash=D(42))
    )
    h = load_holdings(rw_engine)
    assert h.cash == D(42)
    assert set(h.positions) == {"QTUM"}


def test_settings_update_and_validation(rw_engine: Engine, client: TestClient) -> None:
    assert load_settings(rw_engine).selected_profile == "safe"  # default
    r = client.post(
        "/commands/portfolio-settings",
        data={"selected_profile": "aggressive", "whole_shares": "0", "new_cash_only": "1"},
        headers=csrf(client),
    )
    assert r.status_code == 202
    process_commands(rw_engine, handlers=worker_handlers(rw_engine))
    s = load_settings(rw_engine)
    assert (s.selected_profile, s.whole_shares, s.new_cash_only) == ("aggressive", False, True)
    bad = client.post(
        "/commands/portfolio-settings", data={"selected_profile": "yolo"}, headers=csrf(client)
    )
    assert bad.status_code == 400


def test_positions_yaml_imported_once(rw_engine: Engine, tmp_path: Path) -> None:
    seed_universe(rw_engine)
    (tmp_path / "positions.yaml").write_text(
        "holdings:\n  - {symbol: QTUM, shares: 10}\n  - {symbol: NOPE, shares: 3}\n"
        "  - {symbol: ACME, shares: 2.5, cost_basis: 11}\n"
    )
    note = import_positions_yaml(rw_engine, tmp_path)
    assert note and "imported 2 positions" in note and "1 skipped" in note
    h = load_holdings(rw_engine)
    assert set(h.positions) == {"QTUM", "ACME"}
    (tmp_path / "positions.yaml").write_text("holdings:\n  - {symbol: QTUM, shares: 999}\n")
    assert import_positions_yaml(rw_engine, tmp_path) is None  # ignored after the first run
    assert load_holdings(rw_engine).positions["QTUM"].shares == D(10)


def test_holdings_never_reach_llm_calls(rw_engine: Engine) -> None:
    days = seed_prices(rw_engine)
    fake_run(rw_engine, days[-1], {"safe": {"QTUM": 0.9, "ACME": 0.1}})
    apply_holdings_update(
        rw_engine,
        HoldingsUpdate(positions=(PositionIn(symbol="QTUM", shares=D("123.456")),), cash=D(777)),
    )
    run_rebalance(rw_engine, CONFIG)
    with rw_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(rebalance_plans)).scalar() == 1
        assert conn.execute(select(func.count()).select_from(llm_calls)).scalar() == 0
    # No module that could send a prompt imports the holdings tables (none exist before M6);
    # the guard is the import graph: portfolio/* never imports an LLM client.
    import aether.portfolio.holdings as hmod
    import aether.portfolio.publish as rmod

    for mod in (hmod, rmod):
        assert "anthropic" not in Path(mod.__file__ or "").read_text()


def test_worker_handlers_apply_update_and_replan(
    rw_engine: Engine, migrated_db: Path, client: TestClient
) -> None:
    """Through the real scheduler wiring: dashboard POST → command → worker handler → holdings
    + a fresh plan."""
    from aether.jobs import build_scheduler
    from tests.conftest import make_settings

    days = seed_prices(rw_engine)
    fake_run(rw_engine, days[-1], {"safe": {"QTUM": 0.8, "ACME": 0.2}})
    sched = build_scheduler(rw_engine, make_settings(migrated_db))
    assert "tiger_sync" not in {j.id for j in sched.get_jobs()}  # not configured
    r = client.post(
        "/commands/update-holdings",
        data={"shares_QTUM": "10", "cash": "5000"},
        headers=csrf(client),
    )
    assert r.status_code == 202
    sched.get_job("process_commands").func()
    with rw_engine.connect() as conn:
        cmd = conn.execute(select(commands.c.status, commands.c.result)).one()
        plan = conn.execute(select(rebalance_plans.c.plan)).scalar_one()
    assert cmd.status == "done" and json.loads(cmd.result)["replanned"] is True
    assert json.loads(plan)["cash"] == "5000.00"
