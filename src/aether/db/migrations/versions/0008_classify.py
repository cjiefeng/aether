"""M7 classifier: per-ticker direction on event_tickers, classify_state (retries + batch
membership) and eval_runs. The event_tickers rebuild keeps STRICT and WITHOUT ROWID.

Revision ID: 0008_classify
Revises: 0007_news
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_classify"
down_revision = "0007_news"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
STRICT_NO_ROWID = {"sqlite_strict": True, "sqlite_with_rowid": False}


def upgrade() -> None:
    with op.batch_alter_table(
        "event_tickers", recreate="always", table_kwargs=STRICT_NO_ROWID
    ) as batch:
        batch.add_column(sa.Column("direction", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_event_tickers_direction", "direction IS NULL OR direction IN (-1, 0, 1)"
        )

    op.create_table(
        "classify_state",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column("custom_id", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('retry','batched','failed','done')", name="ck_classify_state_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_classify_state_attempts"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name="fk_classify_state_event_id_events",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_classify_state"),
        sa.UniqueConstraint("custom_id", name="uq_classify_state_custom_id"),
        **STRICT,
    )
    op.create_index("ix_classify_state_status", "classify_state", ["status"])
    op.create_index("ix_classify_state_batch_id", "classify_state", ["batch_id"])

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("n_items", sa.Integer(), nullable=False),
        sa.Column("provisional", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("metrics", sa.Text(), nullable=False),
        sa.CheckConstraint("provisional IN (0, 1)", name="ck_eval_runs_provisional_bool"),
        sa.CheckConstraint("passed IN (0, 1)", name="ck_eval_runs_passed_bool"),
        sa.CheckConstraint("json_valid(metrics)", name="ck_eval_runs_metrics_json"),
        sa.PrimaryKeyConstraint("id", name="pk_eval_runs"),
        **STRICT,
    )
    op.create_index("ix_eval_runs_prompt_version", "eval_runs", ["prompt_version"])


def downgrade() -> None:
    op.drop_index("ix_eval_runs_prompt_version", table_name="eval_runs")
    op.drop_table("eval_runs")
    op.drop_index("ix_classify_state_batch_id", table_name="classify_state")
    op.drop_index("ix_classify_state_status", table_name="classify_state")
    op.drop_table("classify_state")
    with op.batch_alter_table(
        "event_tickers", recreate="always", table_kwargs=STRICT_NO_ROWID
    ) as batch:
        batch.drop_constraint("ck_event_tickers_direction", type_="check")
        batch.drop_column("direction")
