"""M11 escalation: `escalations`; research kind `verify`; alert kinds `escalation` and
`escalation_result`. Every rebuild keeps STRICT via table_kwargs.

Revision ID: 0013_escalation
Revises: 0012_config_change_trigger
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_escalation"
down_revision = "0012_config_change_trigger"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
ALERT_KINDS_M10 = (
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
    "weekly_brief",
)
ALERT_KINDS_M11 = (*ALERT_KINDS_M10, "escalation", "escalation_result")
RESEARCH_KINDS_M6 = ("sweep", "backfill")
RESEARCH_KINDS_M11 = (*RESEARCH_KINDS_M6, "verify")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


def _alert_kinds(kinds: tuple[str, ...]) -> None:
    with op.batch_alter_table("alerts", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_alerts_kind", type_="check")
        batch.create_check_constraint("ck_alerts_kind", f"kind IN {_in(kinds)}")


def _research_kinds(kinds: tuple[str, ...]) -> None:
    with op.batch_alter_table("research_runs", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_research_runs_kind", type_="check")
        batch.create_check_constraint("ck_research_runs_kind", f"kind IN {_in(kinds)}")


def upgrade() -> None:
    t = "escalations"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("refusal", sa.Text(), nullable=True),
        sa.Column("research_run_id", sa.Integer(), nullable=True),
        sa.Column("conclusion_id", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.CheckConstraint("trigger IN ('materiality','t1_risk')", name=f"ck_{t}_trigger"),
        sa.CheckConstraint(
            "status IN ('running','done','failed','refused')", name=f"ck_{t}_status"
        ),
        sa.CheckConstraint(
            "refusal IS NULL OR refusal IN ('daily_cap','ticker_cooldown')",
            name=f"ck_{t}_refusal",
        ),
        sa.CheckConstraint(
            "(status = 'refused') = (refusal IS NOT NULL)", name=f"ck_{t}_refused_has_reason"
        ),
        sa.CheckConstraint("json_valid(detail)", name=f"ck_{t}_detail_json"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], name=f"fk_{t}_event_id_events"),
        sa.ForeignKeyConstraint(["symbol"], ["tickers.symbol"], name=f"fk_{t}_symbol_tickers"),
        sa.ForeignKeyConstraint(
            ["research_run_id"], ["research_runs.id"], name=f"fk_{t}_research_run_id_research_runs"
        ),
        sa.ForeignKeyConstraint(
            ["conclusion_id"], ["conclusions.id"], name=f"fk_{t}_conclusion_id_conclusions"
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("event_id", "symbol", name=f"uq_{t}_event_id_symbol"),
        sqlite_strict=True,
    )
    op.create_index(f"ix_{t}_created_at", t, ["created_at"])
    op.create_index(f"ix_{t}_symbol_created_at", t, ["symbol", "created_at"])
    _research_kinds(RESEARCH_KINDS_M11)
    _alert_kinds(ALERT_KINDS_M11)


def downgrade() -> None:
    op.execute("DELETE FROM alerts WHERE kind IN ('escalation','escalation_result')")
    _alert_kinds(ALERT_KINDS_M10)
    op.drop_index("ix_escalations_symbol_created_at", table_name="escalations")
    op.drop_index("ix_escalations_created_at", table_name="escalations")
    op.drop_table("escalations")
    # llm_calls rows reference research runs; keep them and drop only the verify runs' link.
    op.execute(
        "UPDATE llm_calls SET research_run_id = NULL WHERE research_run_id IN "
        "(SELECT id FROM research_runs WHERE kind = 'verify')"
    )
    op.execute("DELETE FROM research_runs WHERE kind = 'verify'")
    _research_kinds(RESEARCH_KINDS_M6)
