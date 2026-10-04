"""Table definitions (SQLAlchemy Core). All tables are STRICT (spec §2.1).

STRICT tables accept only INTEGER/REAL/TEXT/BLOB/ANY, so columns use only `Integer`, `REAL`,
`Text`, `LargeBinary` and `Micros`. Never use String/Boolean/Float/DateTime here: they render
VARCHAR/BOOLEAN/FLOAT/DATETIME and the CREATE TABLE fails.

Each milestone adds its own tables plus a migration (M0: infra, M1: market data). Keep this
file and `migrations/versions/*` in sync (a test compares them).
"""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)

from aether.db.types import Micros

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING)


def _json_ck(col: str, nullable: bool = False) -> CheckConstraint:
    expr = f"json_valid({col})"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"{col}_json")


def _bool_ck(col: str) -> CheckConstraint:
    return CheckConstraint(f"{col} IN (0, 1)", name=f"{col}_bool")


tickers = Table(
    "tickers",
    metadata,
    Column("symbol", Text, primary_key=True),
    Column("name", Text),
    Column("type", Text, nullable=False),
    Column("cik", Text),
    Column("active", Integer, nullable=False, server_default="1"),
    CheckConstraint("type IN ('etf','pure_play','benchmark','context')", name="type"),
    _bool_ck("active"),
    sqlite_strict=True,
)

facts = Table(
    "facts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("claim", Text, nullable=False),
    Column("source_urls", Text, nullable=False),  # JSON array of http(s) URLs
    Column("retrieved_at", Text),
    Column("status", Text, nullable=False),
    Column("notes", Text),
    Column("synced_at", Text, nullable=False),
    CheckConstraint("status IN ('unverified','verified_by_claude','signed_off')", name="status"),
    _json_ck("source_urls"),
    sqlite_strict=True,
)

commands = Table(
    "commands",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("args", Text, nullable=False, server_default="{}"),
    Column("requested_at", Text, nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("processed_at", Text),
    Column("result", Text),
    CheckConstraint("length(kind) BETWEEN 1 AND 64", name="kind_len"),
    CheckConstraint("status IN ('pending','running','done','failed','rejected')", name="status"),
    _json_ck("args"),
    _json_ck("result", nullable=True),
    Index(None, "requested_at"),
    Index(None, "status", "id"),
    sqlite_strict=True,
)

job_runs = Table(
    "job_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("job", Text, nullable=False),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("status", Text, nullable=False),
    Column("rows_written", Integer),
    Column("provider", Text),
    Column("error", Text),
    CheckConstraint("status IN ('running','ok','failed')", name="status"),
    Index(None, "job", "started_at"),
    sqlite_strict=True,
)

llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("purpose", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cache_read_tokens", Integer, nullable=False, server_default="0"),
    Column("web_searches", Integer, nullable=False, server_default="0"),
    Column("cost_micros", Micros, nullable=False, server_default="0"),
    Column("created_at", Text, nullable=False),
    Index(None, "created_at"),
    sqlite_strict=True,
)

alerts = Table(
    "alerts",
    metadata,
    Column("id", Integer, primary_key=True),
    # FK to events(id) is added by the migration that creates `events` (M4).
    Column("event_id", Integer),
    Column("kind", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("sent_at", Text),
    Column("payload", Text, nullable=False, server_default="{}"),
    Column("dedupe_key", Text, nullable=False, unique=True),
    CheckConstraint("channel IN ('telegram','dashboard')", name="channel"),
    _json_ck("payload"),
    sqlite_strict=True,
)

# --------------------------------------------------------------------------- M1: market data

PRICE_PROVIDERS = ("yfinance", "massive", "synthetic")

prices_daily = Table(
    "prices_daily",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("d", Text, nullable=False),  # trading day, YYYY-MM-DD (US/Eastern session date)
    Column("o", REAL, nullable=False),
    Column("h", REAL, nullable=False),
    Column("l", REAL, nullable=False),
    Column("c", REAL, nullable=False),
    Column("volume", Integer, nullable=False),
    Column("provider", Text, nullable=False),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("symbol", "d"),
    CheckConstraint(
        "provider IN (" + ",".join(f"'{p}'" for p in PRICE_PROVIDERS) + ")", name="provider"
    ),
    CheckConstraint("o > 0 AND h > 0 AND l > 0 AND c > 0 AND h >= l", name="ohlc"),
    CheckConstraint("volume >= 0", name="volume"),
    CheckConstraint("d GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", name="d_format"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

qtum_holdings = Table(
    "qtum_holdings",
    metadata,
    Column("snapshot_date", Text, nullable=False),  # the issuer's "data as of" date
    Column("holding_symbol", Text, nullable=False),  # issuer's ticker text, e.g. "3443 TT"
    Column("name", Text),
    Column("cusip", Text),
    Column("weight", REAL, nullable=False),  # percent of fund, e.g. 1.09
    Column("shares", Integer),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("snapshot_date", "holding_symbol"),
    CheckConstraint("weight >= -100 AND weight <= 100", name="weight"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)
