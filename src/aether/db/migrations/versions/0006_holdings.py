"""M5 holdings: holdings, holdings_history, portfolio_settings, profile_targets (monthly published
targets with the research overlay), rebalance_plans, fx_rates, options_snapshots, review_packs; and
two new alert kinds (off_cycle_review, review_pack) via a STRICT-preserving rebuild of `alerts`.

Revision ID: 0006_holdings
Revises: 0005_portfolio
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_holdings"
down_revision = "0005_portfolio"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
STRICT_NO_ROWID = {"sqlite_strict": True, "sqlite_with_rowid": False}
ALERT_KINDS_M3 = (
    "risk_event",
    "insider_cluster",
    "lockup_reminder",
    "earnings_reminder",
    "job_failing",
    "job_recovered",
    "test",
)
ALERT_KINDS_M5 = (*ALERT_KINDS_M3, "off_cycle_review", "review_pack")
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
PROFILES_IN = "profile IN ('safe','medium','aggressive')"
SETTING_KEYS = (
    "'selected_profile','whole_shares','new_cash_only','holdings_source','tiger_sync',"
    "'positions_imported'"
)


def upgrade() -> None:
    op.create_table(
        "holdings",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("shares_micros", sa.Integer(), nullable=False),
        sa.Column("cost_basis_micros", sa.Integer(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("shares_micros >= 0", name="ck_holdings_shares"),
        sa.CheckConstraint(
            "cost_basis_micros IS NULL OR cost_basis_micros >= 0", name="ck_holdings_cost_basis"
        ),
        sa.CheckConstraint(
            "symbol != '$CASH' OR (cost_basis_micros IS NULL AND source = 'manual')",
            name="ck_holdings_cash",
        ),
        sa.CheckConstraint("source IN ('manual','tiger')", name="ck_holdings_source"),
        sa.PrimaryKeyConstraint("symbol", name="pk_holdings"),
        **STRICT,
    )

    op.create_table(
        "holdings_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("command_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("before", sa.Text(), nullable=False),
        sa.Column("after", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source IN ('manual','tiger','import')", name="ck_holdings_history_source"
        ),
        sa.CheckConstraint("json_valid(before)", name="ck_holdings_history_before_json"),
        sa.CheckConstraint("json_valid(after)", name="ck_holdings_history_after_json"),
        sa.ForeignKeyConstraint(
            ["command_id"], ["commands.id"], name="fk_holdings_history_command_id_commands"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_holdings_history"),
        **STRICT,
    )
    op.create_index("ix_holdings_history_applied_at", "holdings_history", ["applied_at"])

    op.create_table(
        "portfolio_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(f"key IN ({SETTING_KEYS})", name="ck_portfolio_settings_key"),
        sa.CheckConstraint("json_valid(value)", name="ck_portfolio_settings_value_json"),
        sa.PrimaryKeyConstraint("key", name="pk_portfolio_settings"),
        **STRICT,
    )

    op.create_table(
        "profile_targets",
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Text(), nullable=False),
        sa.Column("prices_as_of", sa.Text(), nullable=False),
        sa.Column("strategy_run_id", sa.Integer(), nullable=True),
        sa.Column("strategy_id", sa.Text(), nullable=True),
        sa.Column("base_weights", sa.Text(), nullable=False),
        sa.Column("published_weights", sa.Text(), nullable=False),
        sa.Column("adjustments", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("trigger_event_id", sa.Integer(), nullable=True),
        sa.Column("input_hash", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(PROFILES_IN, name="ck_profile_targets_profile"),
        sa.CheckConstraint("trigger IN ('monthly','off_cycle')", name="ck_profile_targets_trigger"),
        sa.CheckConstraint(f"as_of GLOB {DATE_GLOB}", name="ck_profile_targets_as_of_date"),
        sa.CheckConstraint(
            f"prices_as_of GLOB {DATE_GLOB}", name="ck_profile_targets_prices_as_of_date"
        ),
        sa.CheckConstraint("json_valid(base_weights)", name="ck_profile_targets_base_weights_json"),
        sa.CheckConstraint(
            "json_valid(published_weights)", name="ck_profile_targets_published_weights_json"
        ),
        sa.CheckConstraint("json_valid(adjustments)", name="ck_profile_targets_adjustments_json"),
        sa.CheckConstraint("length(input_hash) = 32", name="ck_profile_targets_input_hash_len"),
        sa.ForeignKeyConstraint(
            ["strategy_run_id"],
            ["strategy_runs.id"],
            name="fk_profile_targets_strategy_run_id_strategy_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_event_id"], ["events.id"], name="fk_profile_targets_trigger_event_id_events"
        ),
        sa.PrimaryKeyConstraint("profile", "as_of", name="pk_profile_targets"),
        **STRICT,
    )

    op.create_table(
        "rebalance_plans",
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.LargeBinary(), nullable=False),
        sa.Column("plan", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(PROFILES_IN, name="ck_rebalance_plans_profile"),
        sa.CheckConstraint(f"as_of GLOB {DATE_GLOB}", name="ck_rebalance_plans_as_of_date"),
        sa.CheckConstraint("length(input_hash) = 32", name="ck_rebalance_plans_input_hash_len"),
        sa.CheckConstraint("json_valid(plan)", name="ck_rebalance_plans_plan_json"),
        sa.PrimaryKeyConstraint("profile", "as_of", name="pk_rebalance_plans"),
        **STRICT,
    )

    op.create_table(
        "fx_rates",
        sa.Column("pair", sa.Text(), nullable=False),
        sa.Column("d", sa.Text(), nullable=False),
        sa.Column("rate", sa.REAL(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.CheckConstraint("pair = 'USDSGD'", name="ck_fx_rates_pair"),
        sa.CheckConstraint("rate > 0", name="ck_fx_rates_rate"),
        sa.CheckConstraint(
            "provider IN ('yfinance','ecb','synthetic')", name="ck_fx_rates_provider"
        ),
        sa.CheckConstraint(f"d GLOB {DATE_GLOB}", name="ck_fx_rates_d_date"),
        sa.PrimaryKeyConstraint("pair", "d", name="pk_fx_rates"),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "options_snapshots",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("d", sa.Text(), nullable=False),
        sa.Column("metrics", sa.Text(), nullable=False),
        sa.Column("quality", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('yfinance','synthetic')", name="ck_options_snapshots_provider"
        ),
        sa.CheckConstraint(f"d GLOB {DATE_GLOB}", name="ck_options_snapshots_d_date"),
        sa.CheckConstraint("json_valid(metrics)", name="ck_options_snapshots_metrics_json"),
        sa.CheckConstraint("json_valid(quality)", name="ck_options_snapshots_quality_json"),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_options_snapshots_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("symbol", "d", name="pk_options_snapshots"),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "review_packs",
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("telegram_text", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('done','failed')", name="ck_review_packs_status"),
        sa.CheckConstraint(f"as_of GLOB {DATE_GLOB}", name="ck_review_packs_as_of_date"),
        sa.CheckConstraint(
            "month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'", name="ck_review_packs_month"
        ),
        sa.CheckConstraint(
            "telegram_text IS NULL OR length(telegram_text) <= 4096",
            name="ck_review_packs_telegram_len",
        ),
        sa.CheckConstraint("json_valid(payload)", name="ck_review_packs_payload_json"),
        sa.PrimaryKeyConstraint("as_of", name="pk_review_packs"),
        **STRICT,
    )
    op.create_index("ix_review_packs_month", "review_packs", ["month"])

    _alert_kinds(ALERT_KINDS_M5)


def _alert_kinds(kinds: tuple[str, ...]) -> None:
    # Batch rebuild of `alerts` (rows kept), STRICT preserved via table_kwargs.
    with op.batch_alter_table("alerts", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_alerts_kind", type_="check")
        batch.create_check_constraint(
            "ck_alerts_kind", "kind IN (" + ",".join(f"'{k}'" for k in kinds) + ")"
        )


def downgrade() -> None:
    _alert_kinds(ALERT_KINDS_M3)
    op.drop_index("ix_review_packs_month", table_name="review_packs")
    op.drop_table("review_packs")
    op.drop_table("options_snapshots")
    op.drop_table("fx_rates")
    op.drop_table("rebalance_plans")
    op.drop_table("profile_targets")
    op.drop_table("portfolio_settings")
    op.drop_index("ix_holdings_history_applied_at", table_name="holdings_history")
    op.drop_table("holdings_history")
    op.drop_table("holdings")
