"""M15 LLM expense log: `llm_calls` gains stop_reason, attempts, latency_ms, effort,
price_version and a JSON `context`; the status set gains refusal, refused_budget, batch_submitted
and batch_result. Existing rows: `budget_refused` -> `refused_budget`, batch `ok` ->
`batch_result`. The rebuild keeps STRICT via table_kwargs.

Revision ID: 0017_llm_expense_log
Revises: 0016_adjacent_thesis
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_llm_expense_log"
down_revision = "0016_adjacent_thesis"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
STATUS_M6 = "status IN ('ok','error','budget_refused')"
STATUS_M15 = "status IN ('ok','error','refusal','refused_budget','batch_submitted','batch_result')"


def upgrade() -> None:
    with op.batch_alter_table("llm_calls", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_llm_calls_status", type_="check")
        batch.add_column(sa.Column("stop_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("attempts", sa.Integer(), server_default="1", nullable=False))
        batch.add_column(sa.Column("latency_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("effort", sa.Text(), nullable=True))
        batch.add_column(sa.Column("price_version", sa.Text(), nullable=True))
        batch.add_column(sa.Column("context", sa.Text(), server_default="{}", nullable=False))
    op.execute("UPDATE llm_calls SET status = 'refused_budget' WHERE status = 'budget_refused'")
    op.execute("UPDATE llm_calls SET status = 'batch_result' WHERE status = 'ok' AND batch = 1")
    with op.batch_alter_table("llm_calls", recreate="always", table_kwargs=STRICT) as batch:
        batch.create_check_constraint("ck_llm_calls_status", STATUS_M15)
        batch.create_check_constraint("ck_llm_calls_context_json", "json_valid(context)")
        batch.create_check_constraint("ck_llm_calls_attempts_pos", "attempts >= 0")


def downgrade() -> None:
    with op.batch_alter_table("llm_calls", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_llm_calls_attempts_pos", type_="check")
        batch.drop_constraint("ck_llm_calls_context_json", type_="check")
        batch.drop_constraint("ck_llm_calls_status", type_="check")
    # Rows the M6 schema can't express: refusals were `ok`, submissions weren't logged.
    op.execute("DELETE FROM llm_calls WHERE status = 'batch_submitted'")
    op.execute("UPDATE llm_calls SET status = 'ok' WHERE status IN ('batch_result', 'refusal')")
    op.execute("UPDATE llm_calls SET status = 'budget_refused' WHERE status = 'refused_budget'")
    with op.batch_alter_table("llm_calls", recreate="always", table_kwargs=STRICT) as batch:
        for col in ("context", "price_version", "effort", "latency_ms", "attempts", "stop_reason"):
            batch.drop_column(col)
        batch.create_check_constraint("ck_llm_calls_status", STATUS_M6)
