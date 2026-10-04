"""M1 market data: prices_daily, qtum_holdings (both STRICT, WITHOUT ROWID).

Revision ID: 0002_market_data
Revises: 0001_baseline
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_market_data"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

STRICT_NO_ROWID = {"sqlite_strict": True, "sqlite_with_rowid": False}


def upgrade() -> None:
    op.create_table(
        "prices_daily",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("d", sa.Text(), nullable=False),
        sa.Column("o", sa.REAL(), nullable=False),
        sa.Column("h", sa.REAL(), nullable=False),
        sa.Column("l", sa.REAL(), nullable=False),
        sa.Column("c", sa.REAL(), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('yfinance','massive','synthetic')", name="ck_prices_daily_provider"
        ),
        sa.CheckConstraint(
            "o > 0 AND h > 0 AND l > 0 AND c > 0 AND h >= l", name="ck_prices_daily_ohlc"
        ),
        sa.CheckConstraint("volume >= 0", name="ck_prices_daily_volume"),
        sa.CheckConstraint(
            "d GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", name="ck_prices_daily_d_format"
        ),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_prices_daily_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("symbol", "d", name="pk_prices_daily"),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "qtum_holdings",
        sa.Column("snapshot_date", sa.Text(), nullable=False),
        sa.Column("holding_symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("cusip", sa.Text(), nullable=True),
        sa.Column("weight", sa.REAL(), nullable=False),
        sa.Column("shares", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.CheckConstraint("weight >= -100 AND weight <= 100", name="ck_qtum_holdings_weight"),
        sa.PrimaryKeyConstraint("snapshot_date", "holding_symbol", name="pk_qtum_holdings"),
        **STRICT_NO_ROWID,
    )


def downgrade() -> None:
    op.drop_table("qtum_holdings")
    op.drop_table("prices_daily")
