"""M4 portfolio: dividends, strategy_runs, strategy_metrics, strategy_weights, strategy_curves.

Revision ID: 0005_portfolio
Revises: 0004_alerts
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_portfolio"
down_revision = "0004_alerts"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
STRICT_NO_ROWID = {"sqlite_strict": True, "sqlite_with_rowid": False}
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def upgrade() -> None:
    op.create_table(
        "dividends",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("ex_date", sa.Text(), nullable=False),
        sa.Column("amount_micros", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), server_default="USD", nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.CheckConstraint("amount_micros > 0", name="ck_dividends_amount"),
        sa.CheckConstraint("currency = 'USD'", name="ck_dividends_currency"),
        sa.CheckConstraint(
            "provider IN ('yfinance','massive','synthetic')", name="ck_dividends_provider"
        ),
        sa.CheckConstraint(f"ex_date GLOB {DATE_GLOB}", name="ck_dividends_ex_date_date"),
        sa.ForeignKeyConstraint(["symbol"], ["tickers.symbol"], name="fk_dividends_symbol_tickers"),
        sa.PrimaryKeyConstraint("symbol", "ex_date", name="pk_dividends"),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "strategy_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.LargeBinary(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(f"as_of GLOB {DATE_GLOB}", name="ck_strategy_runs_as_of_date"),
        sa.CheckConstraint("length(input_hash) = 32", name="ck_strategy_runs_input_hash_len"),
        sa.CheckConstraint("json_valid(config)", name="ck_strategy_runs_config_json"),
        sa.CheckConstraint("json_valid(summary)", name="ck_strategy_runs_summary_json"),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_runs"),
        sa.UniqueConstraint("as_of", "input_hash", name="uq_strategy_runs_as_of_input_hash"),
        **STRICT,
    )

    op.create_table(
        "strategy_metrics",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), nullable=True),
        sa.Column("family", sa.Text(), nullable=True),
        sa.Column("qtum_weight", sa.REAL(), nullable=True),
        sa.Column("metrics", sa.Text(), nullable=False),
        sa.Column("qualifies", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('candidate','benchmark')", name="ck_strategy_metrics_kind"),
        sa.CheckConstraint(
            "profile IS NULL OR profile IN ('safe','medium','aggressive')",
            name="ck_strategy_metrics_profile",
        ),
        sa.CheckConstraint(
            "(kind = 'benchmark') = (profile IS NULL AND family IS NULL AND qtum_weight IS NULL)",
            name="ck_strategy_metrics_kind_fields",
        ),
        sa.CheckConstraint("json_valid(metrics)", name="ck_strategy_metrics_metrics_json"),
        sa.CheckConstraint("json_valid(qualifies)", name="ck_strategy_metrics_qualifies_json"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["strategy_runs.id"],
            name="fk_strategy_metrics_run_id_strategy_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "strategy_id", name="pk_strategy_metrics"),
        **STRICT,
    )

    op.create_table(
        "strategy_weights",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("weight", sa.REAL(), nullable=False),
        sa.CheckConstraint("weight >= 0 AND weight <= 1", name="ck_strategy_weights_weight"),
        sa.ForeignKeyConstraint(
            ["run_id", "strategy_id"],
            ["strategy_metrics.run_id", "strategy_metrics.strategy_id"],
            name="fk_strategy_weights_run_id_strategy_metrics",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "strategy_id", "symbol", name="pk_strategy_weights"),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "strategy_curves",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Text(), nullable=False),
        sa.Column("points", sa.Text(), nullable=False),
        sa.CheckConstraint("json_valid(points)", name="ck_strategy_curves_points_json"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["strategy_runs.id"],
            name="fk_strategy_curves_run_id_strategy_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "series_id", name="pk_strategy_curves"),
        **STRICT,
    )


def downgrade() -> None:
    op.drop_table("strategy_curves")
    op.drop_table("strategy_weights")
    op.drop_table("strategy_metrics")
    op.drop_table("strategy_runs")
    op.drop_table("dividends")
