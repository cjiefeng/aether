"""M6 news & research: feed_state, research_runs; per-source detail on event_sources (syndication);
status/batch/cache-write columns on llm_calls; the llm_budget alert kind. Every rebuild keeps STRICT
(and WITHOUT ROWID for event_sources) via table_kwargs.

Revision ID: 0007_news
Revises: 0006_holdings
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_news"
down_revision = "0006_holdings"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
STRICT_NO_ROWID = {"sqlite_strict": True, "sqlite_with_rowid": False}
ALERT_KINDS_M5 = (
    "risk_event",
    "insider_cluster",
    "lockup_reminder",
    "earnings_reminder",
    "job_failing",
    "job_recovered",
    "test",
    "off_cycle_review",
    "review_pack",
)
ALERT_KINDS_M6 = (*ALERT_KINDS_M5, "llm_budget")
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def upgrade() -> None:
    op.create_table(
        "feed_state",
        sa.Column("feed_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("last_fetched_at", sa.Text(), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("items_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_kept", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("feed_id", name="pk_feed_state"),
        **STRICT,
    )
    op.create_table(
        "research_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("window_start", sa.Text(), nullable=False),
        sa.Column("window_end", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column("custom_id", sa.Text(), nullable=True),
        sa.Column("items_found", sa.Integer(), server_default="0", nullable=False),
        sa.Column("events_new", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_micros", sa.Integer(), server_default="0", nullable=False),
        sa.Column("payload", sa.Text(), server_default="{}", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('sweep','backfill')", name="ck_research_runs_kind"),
        sa.CheckConstraint(
            "status IN ('running','submitted','done','failed','budget_refused')",
            name="ck_research_runs_status",
        ),
        sa.CheckConstraint(
            f"window_start GLOB {DATE_GLOB}", name="ck_research_runs_window_start_date"
        ),
        sa.CheckConstraint(f"window_end GLOB {DATE_GLOB}", name="ck_research_runs_window_end_date"),
        sa.CheckConstraint("window_start <= window_end", name="ck_research_runs_window"),
        sa.CheckConstraint("json_valid(payload)", name="ck_research_runs_payload_json"),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_research_runs_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_runs"),
        sa.UniqueConstraint("custom_id", name="uq_research_runs_custom_id"),
        **STRICT,
    )
    op.create_index("ix_research_runs_kind_status", "research_runs", ["kind", "status"])
    op.create_index("ix_research_runs_batch_id", "research_runs", ["batch_id"])

    # event_sources: per-source detail (rows kept; EDGAR rows get NULLs and syndicated = 0).
    with op.batch_alter_table(
        "event_sources", recreate="always", table_kwargs=STRICT_NO_ROWID
    ) as batch:
        batch.add_column(sa.Column("title", sa.Text(), nullable=True))
        batch.add_column(sa.Column("published_at", sa.Text(), nullable=True))
        batch.add_column(sa.Column("excerpt", sa.Text(), nullable=True))
        batch.add_column(sa.Column("simhash", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("excerpt_simhash", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("syndicated", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("origin", sa.Text(), nullable=True))
        batch.add_column(sa.Column("added_at", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_event_sources_excerpt_len", "excerpt IS NULL OR length(excerpt) <= 600"
        )
        batch.create_check_constraint("ck_event_sources_syndicated_bool", "syndicated IN (0, 1)")
        batch.create_check_constraint(
            "ck_event_sources_origin",
            "origin IS NULL OR origin IN ('rss','edgar','web_search','manual')",
        )

    with op.batch_alter_table("llm_calls", recreate="always", table_kwargs=STRICT) as batch:
        batch.add_column(sa.Column("status", sa.Text(), server_default="ok", nullable=False))
        batch.add_column(sa.Column("batch", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(
            sa.Column("cache_write_tokens", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(sa.Column("request_id", sa.Text(), nullable=True))
        batch.add_column(sa.Column("research_run_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("error", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_llm_calls_status", "status IN ('ok','error','budget_refused')"
        )
        batch.create_check_constraint("ck_llm_calls_batch_bool", "batch IN (0, 1)")
        batch.create_check_constraint(
            "ck_llm_calls_non_negative",
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND web_searches >= 0 AND cost_micros >= 0",
        )
        batch.create_foreign_key(
            "fk_llm_calls_research_run_id_research_runs",
            "research_runs",
            ["research_run_id"],
            ["id"],
        )

    op.create_index("ix_events_simhash", "events", ["simhash"])
    op.create_index(
        "ix_events_source_domain_published_at", "events", ["source_domain", "published_at"]
    )
    _alert_kinds(ALERT_KINDS_M6)


def _alert_kinds(kinds: tuple[str, ...]) -> None:
    with op.batch_alter_table("alerts", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_alerts_kind", type_="check")
        batch.create_check_constraint(
            "ck_alerts_kind", "kind IN (" + ",".join(f"'{k}'" for k in kinds) + ")"
        )


def downgrade() -> None:
    _alert_kinds(ALERT_KINDS_M5)
    op.drop_index("ix_events_source_domain_published_at", table_name="events")
    op.drop_index("ix_events_simhash", table_name="events")
    with op.batch_alter_table("llm_calls", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("fk_llm_calls_research_run_id_research_runs", type_="foreignkey")
        batch.drop_constraint("ck_llm_calls_non_negative", type_="check")
        batch.drop_constraint("ck_llm_calls_batch_bool", type_="check")
        batch.drop_constraint("ck_llm_calls_status", type_="check")
        for col in ("error", "research_run_id", "request_id", "cache_write_tokens", "batch"):
            batch.drop_column(col)
        batch.drop_column("status")
    with op.batch_alter_table(
        "event_sources", recreate="always", table_kwargs=STRICT_NO_ROWID
    ) as batch:
        batch.drop_constraint("ck_event_sources_origin", type_="check")
        batch.drop_constraint("ck_event_sources_syndicated_bool", type_="check")
        batch.drop_constraint("ck_event_sources_excerpt_len", type_="check")
        for col in (
            "added_at",
            "origin",
            "syndicated",
            "excerpt_simhash",
            "simhash",
            "excerpt",
            "published_at",
            "title",
        ):
            batch.drop_column(col)
    op.drop_index("ix_research_runs_batch_id", table_name="research_runs")
    op.drop_index("ix_research_runs_kind_status", table_name="research_runs")
    op.drop_table("research_runs")
    op.drop_table("feed_state")
