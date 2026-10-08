"""M12 universe review: `universe_reviews`, `universe_candidates`, `universe_evidence`; alert kind
`universe_review`. Every rebuild keeps STRICT via table_kwargs.

Revision ID: 0014_universe
Revises: 0013_escalation
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_universe"
down_revision = "0013_escalation"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
ALERT_KINDS_M11 = (
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
)
ALERT_KINDS_M12 = (*ALERT_KINDS_M11, "universe_review")
ACTIONS = "('add','remove','watch','keep','skip')"
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def _in(values: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


def _alert_kinds(kinds: tuple[str, ...]) -> None:
    with op.batch_alter_table("alerts", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_alerts_kind", type_="check")
        batch.create_check_constraint("ck_alerts_kind", f"kind IN {_in(kinds)}")


def upgrade() -> None:
    t = "universe_reviews"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), server_default="{}", nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("cost_micros", sa.Integer(), server_default="0", nullable=False),
        sa.Column("telegram_text", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('monthly','manual')", name=f"ck_{t}_kind"),
        sa.CheckConstraint("status IN ('running','done','failed')", name=f"ck_{t}_status"),
        sa.CheckConstraint(f"as_of GLOB {DATE_GLOB}", name=f"ck_{t}_as_of_date"),
        sa.CheckConstraint("month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'", name=f"ck_{t}_month"),
        sa.CheckConstraint("cost_micros >= 0", name=f"ck_{t}_cost"),
        sa.CheckConstraint(
            "telegram_text IS NULL OR length(telegram_text) <= 4096", name=f"ck_{t}_telegram_len"
        ),
        sa.CheckConstraint("error IS NULL OR length(error) <= 1000", name=f"ck_{t}_error_len"),
        sa.CheckConstraint("json_valid(payload)", name=f"ck_{t}_payload_json"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sqlite_strict=True,
    )
    op.create_index(f"ix_{t}_month_status", t, ["month", "status"])

    t = "universe_candidates"
    op.create_table(
        t,
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("track", sa.Text(), server_default="pure_play", nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("proposed_action", sa.Text(), nullable=True),
        sa.Column("cik", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("overlap", sa.Text(), server_default="{}", nullable=False),
        sa.Column("criteria", sa.Text(), server_default="{}", nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("reasons", sa.Text(), server_default="[]", nullable=False),
        sa.Column("evidence_ids", sa.Text(), server_default="[]", nullable=False),
        sa.Column("gate_note", sa.Text(), nullable=True),
        sa.CheckConstraint("track IN ('pure_play')", name=f"ck_{t}_track"),
        sa.CheckConstraint(f"action IN {ACTIONS}", name=f"ck_{t}_action"),
        sa.CheckConstraint(
            f"proposed_action IS NULL OR proposed_action IN {ACTIONS}",
            name=f"ck_{t}_proposed_action",
        ),
        sa.CheckConstraint(
            "description IS NULL OR length(description) <= 300", name=f"ck_{t}_description_len"
        ),
        sa.CheckConstraint("json_valid(overlap)", name=f"ck_{t}_overlap_json"),
        sa.CheckConstraint("json_valid(criteria)", name=f"ck_{t}_criteria_json"),
        sa.CheckConstraint("json_valid(reasons)", name=f"ck_{t}_reasons_json"),
        sa.CheckConstraint("json_valid(evidence_ids)", name=f"ck_{t}_evidence_ids_json"),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["universe_reviews.id"],
            name=f"fk_{t}_review_id_universe_reviews",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("review_id", "symbol", name=f"pk_{t}"),
        sqlite_strict=True,
    )

    t = "universe_evidence"
    op.create_table(
        t,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("trust_tier", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("published_at", sa.Text(), nullable=True),
        sa.Column("date_source", sa.Text(), nullable=True),
        sa.Column("form", sa.Text(), nullable=True),
        sa.Column("accession", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('business_excerpt','web')", name=f"ck_{t}_kind"),
        sa.CheckConstraint("trust_tier IN ('T1','T2','T3')", name=f"ck_{t}_trust_tier"),
        sa.CheckConstraint("excerpt IS NULL OR length(excerpt) <= 600", name=f"ck_{t}_excerpt_len"),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 500", name=f"ck_{t}_title_len"),
        sa.CheckConstraint(
            "url LIKE 'https://%' OR url LIKE 'http://%'", name=f"ck_{t}_url_scheme"
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["universe_reviews.id"],
            name=f"fk_{t}_review_id_universe_reviews",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sqlite_strict=True,
    )
    op.create_index(f"ix_{t}_review_id_symbol", t, ["review_id", "symbol"])
    _alert_kinds(ALERT_KINDS_M12)


def downgrade() -> None:
    op.execute("DELETE FROM alerts WHERE kind = 'universe_review'")
    _alert_kinds(ALERT_KINDS_M11)
    op.drop_index("ix_universe_evidence_review_id_symbol", table_name="universe_evidence")
    op.drop_table("universe_evidence")
    op.drop_table("universe_candidates")
    op.drop_index("ix_universe_reviews_month_status", table_name="universe_reviews")
    op.drop_table("universe_reviews")
