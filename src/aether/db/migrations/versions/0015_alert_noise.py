"""M13 escalation & alert-noise tuning: `alerts.delivery` (immediate / digest / merged /
dashboard_only), alert kind `digest`, alert status `digested`; escalation triggers
`materiality_5` / `severe_category` and refusal `budget`. Every rebuild keeps STRICT via
table_kwargs.

Revision ID: 0015_alert_noise
Revises: 0014_universe
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_alert_noise"
down_revision = "0014_universe"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
ALERT_KINDS_M12 = (
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
    "escalation",
    "escalation_result",
    "universe_review",
)
ALERT_KINDS_M13 = (*ALERT_KINDS_M12, "digest")
ALERT_STATUSES_M3 = ("pending", "sent", "failed", "expired", "dashboard_only")
ALERT_STATUSES_M13 = (*ALERT_STATUSES_M3, "digested")
DELIVERIES = ("immediate", "digest", "merged", "dashboard_only")
TRIGGERS_M11 = ("materiality", "t1_risk")
TRIGGERS_M13 = (*TRIGGERS_M11, "materiality_5", "severe_category")
REFUSALS_M11 = ("daily_cap", "ticker_cooldown")
REFUSALS_M13 = (*REFUSALS_M11, "budget")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


def _escalation_checks(triggers: tuple[str, ...], refusals: tuple[str, ...]) -> None:
    with op.batch_alter_table("escalations", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_escalations_trigger", type_="check")
        batch.drop_constraint("ck_escalations_refusal", type_="check")
        batch.create_check_constraint("ck_escalations_trigger", f"trigger IN {_in(triggers)}")
        batch.create_check_constraint(
            "ck_escalations_refusal", f"refusal IS NULL OR refusal IN {_in(refusals)}"
        )


def upgrade() -> None:
    with op.batch_alter_table("alerts", recreate="always", table_kwargs=STRICT) as batch:
        batch.add_column(
            sa.Column("delivery", sa.Text(), server_default="immediate", nullable=False)
        )
        batch.drop_constraint("ck_alerts_kind", type_="check")
        batch.drop_constraint("ck_alerts_status", type_="check")
        batch.create_check_constraint("ck_alerts_kind", f"kind IN {_in(ALERT_KINDS_M13)}")
        batch.create_check_constraint("ck_alerts_status", f"status IN {_in(ALERT_STATUSES_M13)}")
        batch.create_check_constraint("ck_alerts_delivery", f"delivery IN {_in(DELIVERIES)}")
    op.execute("UPDATE alerts SET delivery = 'dashboard_only' WHERE status = 'dashboard_only'")
    op.create_index("ix_alerts_delivery_status", "alerts", ["delivery", "status"])
    _escalation_checks(TRIGGERS_M13, REFUSALS_M13)


def downgrade() -> None:
    op.execute(
        "DELETE FROM escalations WHERE trigger IN ('materiality_5','severe_category') "
        "OR refusal = 'budget'"
    )
    _escalation_checks(TRIGGERS_M11, REFUSALS_M11)
    op.execute("DELETE FROM alerts WHERE kind = 'digest'")
    op.execute("UPDATE alerts SET status = 'sent' WHERE status = 'digested'")
    op.drop_index("ix_alerts_delivery_status", table_name="alerts")
    with op.batch_alter_table("alerts", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_alerts_delivery", type_="check")
        batch.drop_constraint("ck_alerts_kind", type_="check")
        batch.drop_constraint("ck_alerts_status", type_="check")
        batch.drop_column("delivery")
        batch.create_check_constraint("ck_alerts_kind", f"kind IN {_in(ALERT_KINDS_M12)}")
        batch.create_check_constraint("ck_alerts_status", f"status IN {_in(ALERT_STATUSES_M3)}")
