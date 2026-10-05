"""Holdings and portfolio settings (spec §1.3, M5).

The dashboard validates an edit with these models and enqueues it as a command; the worker
validates it again and applies it here in one short `write_tx`, with a `holdings_history` row.
Holdings never leave the machine and never go into an LLM prompt (spec §1.3, §10).

Holdings are limited to the strategy universe (QTUM + the pure-plays) plus the USD cash row.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Connection, Engine, delete, insert, select

from aether.config import PROFILES, Profile
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import CASH, holdings, holdings_history, portfolio_settings, tickers
from aether.db.types import utcnow_iso

log = logging.getLogger(__name__)

CORE = "QTUM"
SYMBOL_RE = r"^[A-Z][A-Z0-9.\-]{0,9}$"
MAX_SHARES = Decimal(10) ** 9
MAX_CASH = Decimal(10) ** 12
DEFAULT_PROFILE: Profile = "safe"

HoldingsSource = Literal["manual", "tiger"]


def _six_dp(v: Decimal) -> Decimal:
    if v != v.quantize(Decimal("0.000001")):
        raise ValueError("at most 6 decimal places")
    return v


Shares = Annotated[Decimal, Field(ge=0, le=MAX_SHARES, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(ge=0, le=MAX_CASH, allow_inf_nan=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PositionIn(_Strict):
    symbol: str = Field(pattern=SYMBOL_RE)
    shares: Shares
    cost_basis: Money | None = None  # average cost per share, USD

    @field_validator("shares", "cost_basis")
    @classmethod
    def _dp(cls, v: Decimal | None) -> Decimal | None:
        return v if v is None else _six_dp(v)


class HoldingsUpdate(_Strict):
    """`update_holdings` command args. `positions` replaces every universe row (manual mode);
    in tiger mode only `cash` is applied."""

    positions: tuple[PositionIn, ...] = ()
    cash: Money

    @field_validator("cash")
    @classmethod
    def _dp(cls, v: Decimal) -> Decimal:
        return _six_dp(v)

    @field_validator("positions")
    @classmethod
    def _unique(cls, v: tuple[PositionIn, ...]) -> tuple[PositionIn, ...]:
        syms = [p.symbol for p in v]
        if len(syms) != len(set(syms)):
            raise ValueError("duplicate symbol")
        return v

    def check_universe(self, universe: Iterable[str]) -> None:
        allowed = set(universe)
        outside = sorted(p.symbol for p in self.positions if p.symbol not in allowed)
        if outside:
            raise ValueError(f"not in the strategy universe: {', '.join(outside)}")

    def to_args(self) -> dict[str, Any]:
        out: dict[str, Any] = json.loads(self.model_dump_json())  # Decimals as strings
        return out


class SettingsUpdate(_Strict):
    """`update_portfolio_settings` command args. At least one field."""

    selected_profile: Profile | None = None
    whole_shares: bool | None = None
    new_cash_only: bool | None = None
    holdings_source: HoldingsSource | None = None

    @model_validator(mode="after")
    def _non_empty(self) -> SettingsUpdate:
        if not self.model_dump(exclude_none=True):
            raise ValueError("no setting given")
        return self

    def to_args(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


# --------------------------------------------------------------------------- reads


@dataclass(frozen=True)
class Holding:
    symbol: str
    shares: Decimal
    cost_basis: Decimal | None
    source: str
    updated_at: str


@dataclass(frozen=True)
class Holdings:
    positions: dict[str, Holding]  # universe symbols only, shares > 0
    cash: Decimal
    cash_updated_at: str | None

    @property
    def empty(self) -> bool:
        return not self.positions and self.cash == 0


@dataclass(frozen=True)
class PortfolioSettings:
    selected_profile: Profile = DEFAULT_PROFILE
    whole_shares: bool = True
    new_cash_only: bool = False
    holdings_source: HoldingsSource = "manual"
    tiger_sync: dict[str, Any] | None = None
    positions_imported: str | None = None


def universe_symbols(conn_or_engine: Engine | Connection) -> list[str]:
    """QTUM + active pure-plays: the only symbols holdings may contain."""
    stmt = (
        select(tickers.c.symbol)
        .where(tickers.c.type == "pure_play", tickers.c.active == 1)
        .order_by(tickers.c.symbol)
    )
    if isinstance(conn_or_engine, Engine):
        with conn_or_engine.connect() as conn:
            pure = list(conn.execute(stmt).scalars())
    else:
        pure = list(conn_or_engine.execute(stmt).scalars())
    return [CORE, *pure]


def read_holdings(conn: Connection) -> Holdings:
    positions: dict[str, Holding] = {}
    cash, cash_at = Decimal(0), None
    for r in conn.execute(select(holdings).order_by(holdings.c.symbol)):
        if r.symbol == CASH:
            cash, cash_at = r.shares_micros, r.updated_at
        elif r.shares_micros > 0:
            positions[r.symbol] = Holding(
                r.symbol, r.shares_micros, r.cost_basis_micros, r.source, r.updated_at
            )
    return Holdings(positions, cash, cash_at)


def load_holdings(engine: Engine) -> Holdings:
    with engine.connect() as conn:
        return read_holdings(conn)


def read_settings(conn: Connection) -> PortfolioSettings:
    raw = {
        k: json.loads(v)
        for k, v in conn.execute(select(portfolio_settings.c.key, portfolio_settings.c.value))
    }
    base = PortfolioSettings()
    profile = raw.get("selected_profile", base.selected_profile)
    source = raw.get("holdings_source", base.holdings_source)
    return PortfolioSettings(
        selected_profile=profile if profile in PROFILES else base.selected_profile,
        whole_shares=bool(raw.get("whole_shares", base.whole_shares)),
        new_cash_only=bool(raw.get("new_cash_only", base.new_cash_only)),
        holdings_source=source if source in ("manual", "tiger") else "manual",
        tiger_sync=raw.get("tiger_sync"),
        positions_imported=raw.get("positions_imported"),
    )


def load_settings(engine: Engine) -> PortfolioSettings:
    with engine.connect() as conn:
        return read_settings(conn)


# --------------------------------------------------------------------------- writes (worker)


def snapshot(h: Holdings) -> dict[str, Any]:
    """JSON-safe snapshot for holdings_history."""
    return {
        "cash": str(h.cash),
        "positions": {
            s: {
                "shares": str(p.shares),
                "cost_basis": None if p.cost_basis is None else str(p.cost_basis),
                "source": p.source,
            }
            for s, p in sorted(h.positions.items())
        },
    }


def _history(
    conn: Connection, source: str, before: Holdings, command_id: int | None, now: str
) -> None:
    after = read_holdings(conn)
    conn.execute(
        insert(holdings_history).values(
            command_id=command_id,
            source=source,
            before=json.dumps(snapshot(before), sort_keys=True),
            after=json.dumps(snapshot(after), sort_keys=True),
            applied_at=now,
        )
    )


def set_setting(conn: Connection, key: str, value: Any, now: str) -> None:
    upsert(
        conn,
        portfolio_settings,
        [{"key": key, "value": json.dumps(value, sort_keys=True), "updated_at": now}],
        key_cols=["key"],
    )


def _set_cash(conn: Connection, cash: Decimal, now: str) -> None:
    upsert(
        conn,
        holdings,
        [
            {
                "symbol": CASH,
                "shares_micros": cash,
                "cost_basis_micros": None,
                "source": "manual",
                "updated_at": now,
            }
        ],
        key_cols=["symbol"],
    )


def apply_holdings_update(
    engine: Engine, update: HoldingsUpdate, command_id: int | None = None
) -> dict[str, Any]:
    """Apply an `update_holdings` command. Manual mode replaces every universe row; tiger mode
    keeps the synced rows and applies only the cash."""
    now = utcnow_iso()
    with write_tx(engine) as conn:
        universe = universe_symbols(conn)
        update.check_universe(universe)
        settings = read_settings(conn)
        before = read_holdings(conn)
        _set_cash(conn, update.cash, now)
        applied = "cash"
        if settings.holdings_source == "manual":
            conn.execute(delete(holdings).where(holdings.c.symbol != CASH))
            rows = [
                {
                    "symbol": p.symbol,
                    "shares_micros": p.shares,
                    "cost_basis_micros": p.cost_basis,
                    "source": "manual",
                    "updated_at": now,
                }
                for p in update.positions
                if p.shares > 0
            ]
            if rows:
                conn.execute(insert(holdings), rows)
            applied = "positions+cash"
        _history(conn, "manual", before, command_id, now)
    return {"applied": applied, "positions": len(update.positions)}


def apply_settings_update(engine: Engine, update: SettingsUpdate) -> dict[str, Any]:
    now = utcnow_iso()
    values = update.to_args()
    with write_tx(engine) as conn:
        for key, value in sorted(values.items()):
            set_setting(conn, key, value, now)
    return {"updated": sorted(values)}


def replace_universe_positions(
    conn: Connection,
    positions: dict[str, tuple[Decimal, Decimal | None]],
    source: HoldingsSource,
    command_id: int | None,
    now: str,
) -> None:
    """Replace every universe row with `positions` (symbol -> (shares, cost basis)); cash and
    anything else untouched. Used by the Tiger sync."""
    before = read_holdings(conn)
    conn.execute(delete(holdings).where(holdings.c.symbol != CASH))
    rows = [
        {
            "symbol": s,
            "shares_micros": shares,
            "cost_basis_micros": cost,
            "source": source,
            "updated_at": now,
        }
        for s, (shares, cost) in sorted(positions.items())
        if shares > 0
    ]
    if rows:
        conn.execute(insert(holdings), rows)
    _history(conn, source, before, command_id, now)


# --------------------------------------------------------------------------- positions.yaml


class _LegacyPosition(_Strict):
    symbol: str = Field(pattern=SYMBOL_RE)
    shares: Shares
    cost_basis: Money | None = None


class _LegacyPositions(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    holdings: tuple[_LegacyPosition, ...] = ()


POSITIONS_FILE = "positions.yaml"


def import_positions_yaml(engine: Engine, config_dir: Path) -> str | None:
    """Deprecated `config/positions.yaml`: import its `holdings` once, then ignore the file.
    Symbols outside the strategy universe are skipped. Returns a log note, or None."""
    path = config_dir / POSITIONS_FILE
    if not path.exists():
        return None
    if load_settings(engine).positions_imported is not None:
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            parsed = _LegacyPositions.model_validate(yaml.safe_load(fh) or {})
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # Never log the file's contents.
        return f"{POSITIONS_FILE} not imported: {type(exc).__name__}"
    now = utcnow_iso()
    with write_tx(engine) as conn:
        universe = set(universe_symbols(conn))
        before = read_holdings(conn)
        kept = [p for p in parsed.holdings if p.symbol in universe and p.shares > 0]
        if kept and before.empty:
            conn.execute(
                insert(holdings),
                [
                    {
                        "symbol": p.symbol,
                        "shares_micros": p.shares,
                        "cost_basis_micros": p.cost_basis,
                        "source": "manual",
                        "updated_at": now,
                    }
                    for p in kept
                ],
            )
            _history(conn, "import", before, None, now)
        set_setting(conn, "positions_imported", now, now)
    skipped = len(parsed.holdings) - len(kept)
    if not before.empty:
        return f"{POSITIONS_FILE} ignored: holdings already saved"
    return f"imported {len(kept)} positions from {POSITIONS_FILE} ({skipped} skipped)"
