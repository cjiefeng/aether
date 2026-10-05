"""M5 acceptance (S8): Tiger sync replaces only universe symbols and leaves sleeve cash alone; a
failed sync keeps the last snapshot (stale banner); missing credentials disable the module; the
lint check catches order calls and stray imports; the provider has no order-capable attribute.

There are no Tiger credentials to record real responses, so the SDK boundary is faked with
objects shaped like `tigeropen.trade.domain.position.Position` (synthetic symbols/numbers).
"""

from __future__ import annotations

import base64
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# urllib3 (a tigeropen dependency) probes for IPv6 by creating a local ::1 socket at import
# time. Import it during collection so the no-network test below sees only real calls.
import urllib3.util.connection  # noqa: F401
from scripts import check_broker_readonly
from sqlalchemy import Engine, func, select
from starlette.testclient import TestClient

from aether.db.models import holdings_history
from aether.jobs import make_tiger
from aether.portfolio.holdings import (
    HoldingsUpdate,
    PositionIn,
    SettingsUpdate,
    apply_holdings_update,
    apply_settings_update,
    load_holdings,
    load_settings,
)
from aether.portfolio.tiger_sync import sync_holdings
from aether.providers.tiger import (
    TigerConfig,
    TigerError,
    TigerReadOnly,
    mask_account,
    parse_positions,
)
from tests.conftest import REPO, make_settings
from tests.holdings_data import seed_universe

D = Decimal
# Synthetic key body: random-looking base64, not a real key (built at runtime).
FAKE_KEY = base64.b64encode(bytes(range(256)) * 2).decode()
ORDERISH = re.compile(r"order|trade|place|cancel|modify|client", re.IGNORECASE)


def pos(
    symbol: str,
    qty: float,
    cost: float | None = 10.0,
    sec: str = "STK",
    cur: str = "USD",
    scale_qty: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        account="SYNTHETIC",
        contract=SimpleNamespace(symbol=symbol, sec_type=sec, currency=cur),
        quantity=scale_qty if scale_qty is not None else qty,
        position_qty=qty,
        average_cost=cost,
    )


class FakeTiger:
    def __init__(self, positions: list[Any] | Exception) -> None:
        self.positions = positions
        self.calls: list[dict[str, Any]] = []

    def get_positions(self, **kw: Any) -> list[Any]:
        self.calls.append(kw)
        if isinstance(self.positions, Exception):
            raise self.positions
        return self.positions


def tiger_mode(engine: Engine) -> None:
    seed_universe(engine)
    apply_settings_update(engine, SettingsUpdate(holdings_source="tiger"))


# --------------------------------------------------------------------------- config / provider


def test_missing_or_bad_credentials_disable_module(migrated_db: Path) -> None:
    cases = [
        ({}, "not set"),
        ({"tiger_id": "20150001"}, "incomplete"),
        (
            {"tiger_id": "abc", "tiger_private_key": FAKE_KEY, "tiger_account": "1234567"},
            "TIGER_ID is malformed",
        ),
        (
            {"tiger_id": "20150001", "tiger_private_key": "short", "tiger_account": "1234567"},
            "TIGER_PRIVATE_KEY is malformed",
        ),
        (
            {"tiger_id": "20150001", "tiger_private_key": FAKE_KEY, "tiger_account": "12 34"},
            "TIGER_ACCOUNT is malformed",
        ),
    ]
    for overrides, reason in cases:
        settings = make_settings(migrated_db, **overrides)
        cfg, why = TigerConfig.from_settings(settings)
        assert cfg is None and why and reason in why
        client, off = make_tiger(settings)
        assert client is None and off == why


def test_config_repr_hides_key_and_account(migrated_db: Path) -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\\n" + FAKE_KEY + "\\n-----END RSA PRIVATE KEY-----"
    s = make_settings(
        migrated_db, tiger_id="20150001", tiger_private_key=pem, tiger_account="U9876543"
    )
    cfg, why = TigerConfig.from_settings(s)
    assert cfg is not None and why is None
    assert cfg.private_key == FAKE_KEY  # PEM headers and \n escapes stripped
    assert FAKE_KEY not in repr(cfg) and "U9876543" not in repr(cfg)
    assert mask_account("U9876543") == "••••6543"


def test_connect_builds_sdk_client_without_network(migrated_db: Path) -> None:
    """Real SDK construction (attribute names verified against tigeropen); pytest-socket would
    fail this test on any network call (dynamic-domain lookup is off)."""
    cfg = TigerConfig("20150001", FAKE_KEY, "U9876543")
    t = TigerReadOnly.connect(cfg)
    assert t.account_masked == "••••6543"
    assert getattr(t._get_positions, "__name__", "") == "get_positions"


def test_provider_has_no_order_capable_attribute() -> None:
    t = TigerReadOnly(FakeTiger([]).get_positions, "U9876543")
    public = [a for a in dir(t) if not a.startswith("__")]
    assert sorted(public) == [
        "_account",
        "_get_positions",
        "account_masked",
        "connect",
        "positions",
    ]
    assert not [a for a in public if ORDERISH.search(a)]
    assert TigerReadOnly.__slots__ == ("_account", "_get_positions")
    with pytest.raises(AttributeError):
        t.client = object()  # type: ignore[attr-defined]


def test_parse_positions_uses_decimal_qty_and_filters() -> None:
    got = parse_positions(
        [
            pos("QTUM", 12.5, cost=88.1234567, scale_qty=1250000),
            pos("ACME", 3, sec="OPT"),
            pos("DEMO", 4, cur="HKD"),
            pos("bad sym", 1),
            pos("EXMP", -2),
        ]
    )
    assert got == [type(got[0])("QTUM", D("12.500000"), D("88.123457"))]


def test_errors_are_scrubbed() -> None:
    secret = "account U9876543 holds 500 QTUM"

    class ApiException(Exception):
        code = 1010

    t = TigerReadOnly(FakeTiger(ApiException(secret)).get_positions, "U9876543")
    with pytest.raises(TigerError) as exc:
        t.positions()
    assert str(exc.value) == "Tiger get_positions failed: ApiException (code 1010)"
    assert exc.value.__cause__ is None


# --------------------------------------------------------------------------- sync


def test_sync_replaces_only_universe_symbols_and_keeps_cash(rw_engine: Engine) -> None:
    tiger_mode(rw_engine)
    apply_settings_update(rw_engine, SettingsUpdate(holdings_source="manual"))
    apply_holdings_update(
        rw_engine,
        HoldingsUpdate(
            positions=(
                PositionIn(symbol="QTUM", shares=D(1)),
                PositionIn(symbol="EXMP", shares=D(7)),
            ),
            cash=D("2500"),
        ),
    )
    apply_settings_update(rw_engine, SettingsUpdate(holdings_source="tiger"))
    fake = FakeTiger(
        [pos("QTUM", 40, 90.0), pos("ACME", 5.5, 20.0), pos("NVDA", 3), pos("ZZZZ", 1)]
    )
    t = TigerReadOnly(fake.get_positions, "U9876543")
    result = sync_holdings(rw_engine, t, None, command_id=None)
    assert result.rows_written == 2
    assert fake.calls == [{"sec_type": "STK", "currency": "USD", "market": "US"}]
    h = load_holdings(rw_engine)
    assert {s: p.shares for s, p in h.positions.items()} == {"QTUM": D(40), "ACME": D("5.5")}
    assert all(p.source == "tiger" for p in h.positions.values())
    assert h.cash == D(2500)  # sleeve cash stays manual
    status = load_settings(rw_engine).tiger_sync or {}
    assert status["outside_universe"] == 2 and status["account_masked"] == "••••6543"
    assert status["error"] is None and status["last_ok"]
    with rw_engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(holdings_history)
                .where(holdings_history.c.source == "tiger")
            ).scalar()
            == 1
        )


def test_failed_sync_keeps_snapshot_and_shows_stale(rw_engine: Engine, client: TestClient) -> None:
    tiger_mode(rw_engine)
    ok = TigerReadOnly(FakeTiger([pos("QTUM", 40)]).get_positions, "U9876543")
    sync_holdings(rw_engine, ok, None)
    broken = TigerReadOnly(FakeTiger(ConnectionError("boom")).get_positions, "U9876543")
    with pytest.raises(TigerError):
        sync_holdings(rw_engine, broken, None)
    assert load_holdings(rw_engine).positions["QTUM"].shares == D(40)
    status = load_settings(rw_engine).tiger_sync or {}
    assert status["last_ok"] and "ConnectionError" in status["error"]
    page = client.get("/holdings").text
    assert f"stale since {status['last_ok']}" in page
    assert "The last saved snapshot is in use." in page


def test_missing_credentials_sync_fails_closed(rw_engine: Engine) -> None:
    tiger_mode(rw_engine)
    apply_holdings_update(rw_engine, HoldingsUpdate(cash=D(10)))
    with pytest.raises(TigerError, match="not set"):
        sync_holdings(rw_engine, None, "TIGER_ID / TIGER_PRIVATE_KEY / TIGER_ACCOUNT not set")
    assert load_holdings(rw_engine).cash == D(10)


def test_manual_mode_never_syncs(rw_engine: Engine) -> None:
    seed_universe(rw_engine)
    fake = FakeTiger([pos("QTUM", 40)])
    result = sync_holdings(rw_engine, TigerReadOnly(fake.get_positions, "U9876543"), None)
    assert result.warning and fake.calls == []
    assert load_holdings(rw_engine).empty


# --------------------------------------------------------------------------- lint


def test_lint_catches_planted_order_call_and_stray_import(tmp_path: Path) -> None:
    bad = tmp_path / "aether" / "web" / "evil.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("from tigeropen.trade.trade_client import TradeClient\nc.place_order(o)\n")
    problems = check_broker_readonly.check_file(bad)
    assert any("may only be imported" in p for p in problems)
    assert any("place_order" in p for p in problems)
    ok_file = tmp_path / "aether" / "providers" / "tiger.py"
    ok_file.parent.mkdir(parents=True)
    ok_file.write_text("from tigeropen.trade.trade_client import TradeClient\nc.cancel_order(1)\n")
    assert [p for p in check_broker_readonly.check_file(ok_file) if "imported" in p] == []
    assert any("cancel_order" in p for p in check_broker_readonly.check_file(ok_file))
    rc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_broker_readonly.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 1


def test_src_passes_broker_lint() -> None:
    assert check_broker_readonly.main([str(REPO / "src")]) == 0


def test_scheduler_registers_tiger_job_only_when_configured(
    rw_engine: Engine, migrated_db: Path
) -> None:
    from aether.jobs import build_scheduler

    on = build_scheduler(
        rw_engine,
        make_settings(
            migrated_db, tiger_id="20150001", tiger_private_key=FAKE_KEY, tiger_account="U9876543"
        ),
    )
    assert "tiger_sync" in {j.id for j in on.get_jobs()}
