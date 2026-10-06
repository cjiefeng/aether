"""M10 conclusions, track record, brief: `conclusions`, `conclusion_outcomes`, `overlay_outcomes`,
`briefs`, `conclusion_failures`; alert kind `weekly_brief`.

Revision ID: 0011_conclusions
Revises: 0010_scores
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_conclusions"
down_revision = "0010_scores"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
STRICT_NO_ROWID = {"sqlite_strict": True, "sqlite_with_rowid": False}
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
ALERT_KINDS_M6 = (
    "risk_event",
    "insider_cluster",
    "lockup_reminder",
    "earnings_reminder",
    "job_failing",
    "job_recovered",
    "test",
    "off_cycle_review",
    "review_pack",
    "llm_budget",
)
ALERT_KINDS_M10 = (*ALERT_KINDS_M6, "weekly_brief")
STANCES = "'ACCUMULATE','HOLD','TRIM','AVOID'"
TILTS = "'PURE_PLAYS','NEUTRAL','QTUM'"
HORIZONS = "'1m','3m','6m','12m','24m','36m'"


def _date(table: str, col: str, nullable: bool = False) -> sa.CheckConstraint:
    expr = f"{col} GLOB {DATE_GLOB}"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=f"ck_{table}_{col}_date")


def _json(table: str, col: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"json_valid({col})", name=f"ck_{table}_{col}_json")


def _ck(table: str, name: str, expr: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expr, name=f"ck_{table}_{name}")


def _alert_kinds(kinds: tuple[str, ...]) -> None:
    with op.batch_alter_table("alerts", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_alerts_kind", type_="check")
        batch.create_check_constraint(
            "ck_alerts_kind", "kind IN (" + ",".join(f"'{k}'" for k in kinds) + ")"
        )


def upgrade() -> None:
    t = "conclusions"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("stance", sa.Text(), nullable=False),
        sa.Column("proposed_stance", sa.Text(), nullable=False),
        sa.Column("held", sa.Integer(), server_default="0", nullable=False),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("horizon", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.LargeBinary(), nullable=False),
        sa.Column("cost_micros", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prev_id", sa.Integer(), nullable=True),
        _ck(t, "kind", "kind IN ('ticker','theme')"),
        _ck(t, "symbol_kind", "(kind = 'ticker') = (symbol IS NOT NULL)"),
        _ck(
            t,
            "stance",
            f"(kind = 'ticker' AND stance IN ({STANCES}) AND proposed_stance IN ({STANCES})) OR "
            f"(kind = 'theme' AND stance IN ({TILTS}) AND proposed_stance IN ({TILTS}))",
        ),
        _ck(t, "held_stance", "held = 1 OR stance = proposed_stance"),
        _ck(t, "held_bool", "held IN (0, 1)"),
        _ck(t, "confidence", "confidence >= 0 AND confidence <= 1"),
        _ck(t, "horizon", "horizon IN ('12m','36m')"),
        _date(t, "as_of"),
        _json(t, "payload"),
        _json(t, "evidence"),
        _ck(t, "input_hash_len", "length(input_hash) = 32"),
        _ck(t, "cost", "cost_micros >= 0"),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_conclusions_symbol_tickers"
        ),
        sa.ForeignKeyConstraint(
            ["prev_id"], ["conclusions.id"], name="fk_conclusions_prev_id_conclusions"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conclusions"),
        **STRICT,
    )
    op.create_index("ix_conclusions_symbol_as_of", t, ["symbol", "as_of"])
    op.create_index("ix_conclusions_kind_id", t, ["kind", "id"])

    t = "conclusion_outcomes"
    op.create_table(
        t,
        sa.Column("conclusion_id", sa.Integer(), nullable=False),
        sa.Column("horizon", sa.Text(), nullable=False),
        sa.Column("benchmark", sa.Text(), nullable=False),
        sa.Column("start_d", sa.Text(), nullable=True),
        sa.Column("end_d", sa.Text(), nullable=False),
        sa.Column("excess_return", sa.REAL(), nullable=True),
        sa.Column("hit", sa.Integer(), nullable=True),
        sa.Column("hold_hit", sa.Integer(), nullable=True),
        sa.Column("momentum_stance", sa.Text(), nullable=True),
        sa.Column("momentum_hit", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=False),
        _ck(t, "horizon", f"horizon IN ({HORIZONS})"),
        _ck(t, "status", "status IN ('pending','complete')"),
        _ck(t, "hit_bool", "hit IS NULL OR hit IN (0, 1)"),
        _ck(t, "hold_hit_bool", "hold_hit IS NULL OR hold_hit IN (0, 1)"),
        _ck(t, "momentum_hit_bool", "momentum_hit IS NULL OR momentum_hit IN (0, 1)"),
        _ck(
            t,
            "complete_has_values",
            "status = 'pending' OR (excess_return IS NOT NULL AND hit IS NOT NULL)",
        ),
        _date(t, "start_d", nullable=True),
        _date(t, "end_d"),
        sa.ForeignKeyConstraint(
            ["conclusion_id"],
            ["conclusions.id"],
            name="fk_conclusion_outcomes_conclusion_id_conclusions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conclusion_id", "horizon", name="pk_conclusion_outcomes"),
        **STRICT_NO_ROWID,
    )

    t = "overlay_outcomes"
    op.create_table(
        t,
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("horizon", sa.Text(), nullable=False),
        sa.Column("start_d", sa.Text(), nullable=True),
        sa.Column("end_d", sa.Text(), nullable=False),
        sa.Column("base_return", sa.REAL(), nullable=True),
        sa.Column("adjusted_return", sa.REAL(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=False),
        _ck(t, "horizon", f"horizon IN ({HORIZONS})"),
        _ck(t, "status", "status IN ('pending','complete')"),
        _ck(
            t,
            "complete_has_values",
            "status = 'pending' OR (base_return IS NOT NULL AND adjusted_return IS NOT NULL)",
        ),
        _date(t, "start_d", nullable=True),
        _date(t, "end_d"),
        sa.ForeignKeyConstraint(
            ["profile", "as_of"],
            ["profile_targets.profile", "profile_targets.as_of"],
            name="fk_overlay_outcomes_profile_profile_targets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("profile", "as_of", "horizon", name="pk_overlay_outcomes"),
        **STRICT_NO_ROWID,
    )

    t = "briefs"
    op.create_table(
        t,
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("week", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("telegram_text", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        _ck(t, "status", "status IN ('done','failed')"),
        _date(t, "as_of"),
        _json(t, "payload"),
        sa.PrimaryKeyConstraint("as_of", name="pk_briefs"),
        sa.UniqueConstraint("week", name="uq_briefs_week"),
        **STRICT,
    )

    t = "conclusion_failures"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        _ck(t, "kind", "kind IN ('ticker','theme')"),
        _ck(t, "attempts", "attempts >= 0"),
        _ck(t, "error_len", "length(error) <= 1000"),
        _date(t, "as_of"),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_conclusion_failures_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conclusion_failures"),
        **STRICT,
    )
    op.create_index("ix_conclusion_failures_created_at", t, ["created_at"])

    _alert_kinds(ALERT_KINDS_M10)


def downgrade() -> None:
    _alert_kinds(ALERT_KINDS_M6)
    op.drop_index("ix_conclusion_failures_created_at", table_name="conclusion_failures")
    op.drop_table("conclusion_failures")
    op.drop_table("briefs")
    op.drop_table("overlay_outcomes")
    op.drop_table("conclusion_outcomes")
    op.drop_index("ix_conclusions_kind_id", table_name="conclusions")
    op.drop_index("ix_conclusions_symbol_as_of", table_name="conclusions")
    op.drop_table("conclusions")
