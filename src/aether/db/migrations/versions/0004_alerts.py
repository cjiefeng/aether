"""M3 alerts: rebuild `alerts` as an outbox (status, text, attempts, created_at, kind CHECK) and add
`facts.open_question`.

Nothing wrote `alerts` before M3, so the table is dropped and recreated; the upgrade refuses to
run if it somehow holds rows.

Revision ID: 0004_alerts
Revises: 0003_edgar
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_alerts"
down_revision = "0003_edgar"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}

KINDS = (
    "risk_event",
    "insider_cluster",
    "lockup_reminder",
    "earnings_reminder",
    "job_failing",
    "job_recovered",
    "test",
)
STATUSES = ("pending", "sent", "failed", "expired", "dashboard_only")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    n = op.get_bind().execute(sa.text("SELECT count(*) FROM alerts")).scalar_one()
    if n:
        raise RuntimeError(f"alerts holds {n} rows; 0004 expects it empty")
    op.drop_table("alerts")
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), server_default="{}", nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.CheckConstraint("channel IN ('telegram','dashboard')", name="ck_alerts_channel"),
        sa.CheckConstraint(_in("kind", KINDS), name="ck_alerts_kind"),
        sa.CheckConstraint(_in("status", STATUSES), name="ck_alerts_status"),
        sa.CheckConstraint("length(text) BETWEEN 1 AND 4096", name="ck_alerts_text_len"),
        sa.CheckConstraint("attempts >= 0", name="ck_alerts_attempts"),
        sa.CheckConstraint("json_valid(payload)", name="ck_alerts_payload_json"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], name="fk_alerts_event_id_events"),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sa.UniqueConstraint("dedupe_key", name="uq_alerts_dedupe_key"),
        **STRICT,
    )
    op.create_index("ix_alerts_status_id", "alerts", ["status", "id"])
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])

    with op.batch_alter_table("facts", table_kwargs=STRICT) as batch:
        batch.add_column(sa.Column("open_question", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("facts", table_kwargs=STRICT) as batch:
        batch.drop_column("open_question")
    op.drop_index("ix_alerts_created_at", table_name="alerts")
    op.drop_index("ix_alerts_status_id", table_name="alerts")
    op.drop_table("alerts")
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), server_default="{}", nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.CheckConstraint("channel IN ('telegram','dashboard')", name="ck_alerts_channel"),
        sa.CheckConstraint("json_valid(payload)", name="ck_alerts_payload_json"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], name="fk_alerts_event_id_events"),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sa.UniqueConstraint("dedupe_key", name="uq_alerts_dedupe_key"),
        **STRICT,
    )
