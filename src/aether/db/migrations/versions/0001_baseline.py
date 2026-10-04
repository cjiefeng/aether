"""M0 baseline: infra tables (tickers, facts, commands, job_runs, llm_calls, alerts).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}


def upgrade() -> None:
    op.create_table(
        "tickers",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("cik", sa.Text(), nullable=True),
        sa.Column("active", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "type IN ('etf','pure_play','benchmark','context')", name="ck_tickers_type"
        ),
        sa.CheckConstraint("active IN (0, 1)", name="ck_tickers_active_bool"),
        sa.PrimaryKeyConstraint("symbol", name="pk_tickers"),
        **STRICT,
    )

    op.create_table(
        "facts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("source_urls", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('unverified','verified_by_claude','signed_off')", name="ck_facts_status"
        ),
        sa.CheckConstraint("json_valid(source_urls)", name="ck_facts_source_urls_json"),
        sa.PrimaryKeyConstraint("id", name="pk_facts"),
        **STRICT,
    )

    op.create_table(
        "commands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("args", sa.Text(), server_default="{}", nullable=False),
        sa.Column("requested_at", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("processed_at", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.CheckConstraint("length(kind) BETWEEN 1 AND 64", name="ck_commands_kind_len"),
        sa.CheckConstraint(
            "status IN ('pending','running','done','failed','rejected')",
            name="ck_commands_status",
        ),
        sa.CheckConstraint("json_valid(args)", name="ck_commands_args_json"),
        sa.CheckConstraint("result IS NULL OR json_valid(result)", name="ck_commands_result_json"),
        sa.PrimaryKeyConstraint("id", name="pk_commands"),
        **STRICT,
    )
    op.create_index("ix_commands_requested_at", "commands", ["requested_at"])
    op.create_index("ix_commands_status_id", "commands", ["status", "id"])

    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows_written", sa.Integer(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('running','ok','failed')", name="ck_job_runs_status"),
        sa.PrimaryKeyConstraint("id", name="pk_job_runs"),
        **STRICT,
    )
    op.create_index("ix_job_runs_job_started_at", "job_runs", ["job", "started_at"])

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("web_searches", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_micros", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
        **STRICT,
    )
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])

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
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sa.UniqueConstraint("dedupe_key", name="uq_alerts_dedupe_key"),
        **STRICT,
    )


def downgrade() -> None:
    for table in ("alerts", "llm_calls", "job_runs", "commands", "facts", "tickers"):
        op.drop_table(table)
