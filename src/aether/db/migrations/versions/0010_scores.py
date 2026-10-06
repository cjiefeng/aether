"""M9 scorecards, reactions, theme: `xbrl_fetches`, `scorecards`, `theme_decomposition`,
`event_reactions`, `calibration_reports`.

Revision ID: 0010_scores
Revises: 0009_catalysts
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_scores"
down_revision = "0009_catalysts"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
STRICT_NO_ROWID = {"sqlite_strict": True, "sqlite_with_rowid": False}
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def _date(table: str, col: str, nullable: bool = False) -> sa.CheckConstraint:
    expr = f"{col} GLOB {DATE_GLOB}"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=f"ck_{table}_{col}_date")


def _json(table: str, col: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"json_valid({col})", name=f"ck_{table}_{col}_json")


def upgrade() -> None:
    op.create_table(
        "xbrl_fetches",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_xbrl_fetches_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("symbol", name="pk_xbrl_fetches"),
        **STRICT,
    )

    op.create_table(
        "scorecards",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("components", sa.Text(), nullable=False),
        sa.Column("total", sa.REAL(), nullable=True),
        sa.Column("coverage", sa.REAL(), nullable=False),
        sa.Column("input_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "total IS NULL OR (total >= -100 AND total <= 100)", name="ck_scorecards_total"
        ),
        sa.CheckConstraint("coverage >= 0 AND coverage <= 1", name="ck_scorecards_coverage"),
        _date("scorecards", "as_of"),
        _json("scorecards", "components"),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_scorecards_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("symbol", "as_of", name="pk_scorecards"),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "theme_decomposition",
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("n_sessions", sa.Integer(), nullable=False),
        sa.Column("betas", sa.Text(), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=False),
        sa.Column("r2", sa.REAL(), nullable=True),
        sa.Column("quantum_partial_r2", sa.REAL(), nullable=True),
        sa.Column("watchlist_weight_in_qtum", sa.REAL(), nullable=True),
        sa.Column("basket_members", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        _date("theme_decomposition", "as_of"),
        _json("theme_decomposition", "betas"),
        _json("theme_decomposition", "attribution"),
        _json("theme_decomposition", "basket_members"),
        sa.PrimaryKeyConstraint("as_of", name="pk_theme_decomposition"),
        **STRICT,
    )

    op.create_table(
        "event_reactions",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("t0", sa.Text(), nullable=True),
        sa.Column("benchmark", sa.Text(), nullable=False),
        sa.Column("beta", sa.REAL(), nullable=True),
        sa.Column("sigma_resid", sa.REAL(), nullable=True),
        sa.Column("beta_fallback", sa.Integer(), server_default="0", nullable=False),
        sa.Column("car_1", sa.REAL(), nullable=True),
        sa.Column("car_5", sa.REAL(), nullable=True),
        sa.Column("car_20", sa.REAL(), nullable=True),
        sa.Column("z_1", sa.REAL(), nullable=True),
        sa.Column("z_5", sa.REAL(), nullable=True),
        sa.Column("z_20", sa.REAL(), nullable=True),
        sa.Column("ret_raw_1", sa.REAL(), nullable=True),
        sa.Column("abn_volume", sa.REAL(), nullable=True),
        sa.Column("reversal_ratio", sa.REAL(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("confounders", sa.Text(), server_default="[]", nullable=False),
        sa.Column("approx_time", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','complete','confounded','no_data')",
            name="ck_event_reactions_status",
        ),
        sa.CheckConstraint("beta_fallback IN (0, 1)", name="ck_event_reactions_beta_fallback_bool"),
        sa.CheckConstraint("approx_time IN (0, 1)", name="ck_event_reactions_approx_time_bool"),
        _date("event_reactions", "t0", nullable=True),
        sa.CheckConstraint(
            "t0 IS NOT NULL OR status = 'no_data'", name="ck_event_reactions_t0_required"
        ),
        _json("event_reactions", "confounders"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name="fk_event_reactions_event_id_events",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_event_reactions_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("event_id", "symbol", name="pk_event_reactions"),
        **STRICT_NO_ROWID,
    )
    op.create_index("ix_event_reactions_symbol_t0", "event_reactions", ["symbol", "t0"])
    op.create_index("ix_event_reactions_status", "event_reactions", ["status"])

    op.create_table(
        "calibration_reports",
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        _date("calibration_reports", "as_of"),
        _json("calibration_reports", "payload"),
        sa.PrimaryKeyConstraint("as_of", name="pk_calibration_reports"),
        **STRICT,
    )


def downgrade() -> None:
    op.drop_table("calibration_reports")
    op.drop_index("ix_event_reactions_status", table_name="event_reactions")
    op.drop_index("ix_event_reactions_symbol_t0", table_name="event_reactions")
    op.drop_table("event_reactions")
    op.drop_table("theme_decomposition")
    op.drop_table("scorecards")
    op.drop_table("xbrl_fetches")
