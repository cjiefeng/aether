"""M8 catalysts + market structure: `catalysts`, `short_interest`, `short_interest_files`, and the
`finra` event origin (short-interest spike rule events).

`events` is rebuilt to widen its origin CHECK. Migrations run with foreign keys off (see env.py),
so dropping the old `events` table doesn't cascade into its children; env.py runs
`PRAGMA foreign_key_check` afterwards.

Revision ID: 0009_catalysts
Revises: 0008_classify
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_catalysts"
down_revision = "0008_classify"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
STRICT_NO_ROWID = {"sqlite_strict": True, "sqlite_with_rowid": False}
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"

ORIGINS_M7 = ("rss", "edgar", "web_search", "manual")
ORIGINS_M8 = (*ORIGINS_M7, "finra")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


def _origins(origins: tuple[str, ...]) -> None:
    with op.batch_alter_table("events", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_events_origin", type_="check")
        batch.create_check_constraint("ck_events_origin", f"origin IN {_in(origins)}")
    with op.batch_alter_table(
        "event_sources", recreate="always", table_kwargs=STRICT_NO_ROWID
    ) as batch:
        batch.drop_constraint("ck_event_sources_origin", type_="check")
        batch.create_check_constraint(
            "ck_event_sources_origin", f"origin IS NULL OR origin IN {_in(origins)}"
        )


def upgrade() -> None:
    _origins(ORIGINS_M8)

    op.create_table(
        "catalysts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("window_start", sa.Text(), nullable=False),
        sa.Column("window_end", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="upcoming", nullable=False),
        sa.Column("fact_id", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("keywords", sa.Text(), server_default="[]", nullable=False),
        sa.Column("resolve_categories", sa.Text(), server_default="[]", nullable=False),
        sa.Column("resolved_by_event_id", sa.Integer(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("origin IN ('seed','earnings','lockup')", name="ck_catalysts_origin"),
        sa.CheckConstraint(
            "kind IN ('roadmap','program','earnings','lockup','regulatory')",
            name="ck_catalysts_kind",
        ),
        sa.CheckConstraint(
            "status IN ('upcoming','hit','slipped','cancelled')", name="ck_catalysts_status"
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN "
            "('event','date','window_passed','owner','rescheduled')",
            name="ck_catalysts_resolution",
        ),
        sa.CheckConstraint(
            "(status = 'upcoming') = (resolution IS NULL)", name="ck_catalysts_resolved"
        ),
        sa.CheckConstraint(f"window_start GLOB {DATE_GLOB}", name="ck_catalysts_window_start_date"),
        sa.CheckConstraint(
            f"window_end IS NULL OR window_end GLOB {DATE_GLOB}",
            name="ck_catalysts_window_end_date",
        ),
        sa.CheckConstraint(
            "window_end IS NULL OR window_start <= window_end", name="ck_catalysts_window"
        ),
        sa.CheckConstraint("note IS NULL OR length(note) <= 200", name="ck_catalysts_note_len"),
        sa.CheckConstraint("json_valid(keywords)", name="ck_catalysts_keywords_json"),
        sa.CheckConstraint(
            "json_valid(resolve_categories)", name="ck_catalysts_resolve_categories_json"
        ),
        sa.ForeignKeyConstraint(["symbol"], ["tickers.symbol"], name="fk_catalysts_symbol_tickers"),
        sa.ForeignKeyConstraint(["fact_id"], ["facts.id"], name="fk_catalysts_fact_id_facts"),
        sa.ForeignKeyConstraint(
            ["resolved_by_event_id"],
            ["events.id"],
            name="fk_catalysts_resolved_by_event_id_events",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalysts"),
        sa.UniqueConstraint("key", name="uq_catalysts_key"),
        **STRICT,
    )
    op.create_index("ix_catalysts_status_window_start", "catalysts", ["status", "window_start"])
    op.create_index("ix_catalysts_symbol", "catalysts", ["symbol"])

    op.create_table(
        "short_interest",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("settlement_date", sa.Text(), nullable=False),
        sa.Column("short_shares", sa.Integer(), nullable=False),
        sa.Column("prev_short_shares", sa.Integer(), nullable=True),
        sa.Column("avg_daily_volume", sa.Integer(), nullable=True),
        sa.Column("days_to_cover", sa.REAL(), nullable=True),
        sa.Column("shares_out", sa.Integer(), nullable=True),
        sa.Column("shares_out_as_of", sa.Text(), nullable=True),
        sa.Column("pct_shares_out", sa.REAL(), nullable=True),
        sa.Column("source", sa.Text(), server_default="finra", nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.CheckConstraint("short_shares >= 0", name="ck_short_interest_short_shares"),
        sa.CheckConstraint(
            "shares_out IS NULL OR shares_out > 0", name="ck_short_interest_shares_out"
        ),
        sa.CheckConstraint("source IN ('finra','synthetic')", name="ck_short_interest_source"),
        sa.CheckConstraint(
            f"settlement_date GLOB {DATE_GLOB}", name="ck_short_interest_settlement_date_date"
        ),
        sa.CheckConstraint(
            f"shares_out_as_of IS NULL OR shares_out_as_of GLOB {DATE_GLOB}",
            name="ck_short_interest_shares_out_as_of_date",
        ),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_short_interest_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("symbol", "settlement_date", name="pk_short_interest"),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "short_interest_files",
        sa.Column("settlement_date", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"settlement_date GLOB {DATE_GLOB}",
            name="ck_short_interest_files_settlement_date_date",
        ),
        sa.PrimaryKeyConstraint("settlement_date", name="pk_short_interest_files"),
        **STRICT,
    )


def downgrade() -> None:
    op.drop_table("short_interest_files")
    op.drop_table("short_interest")
    op.drop_index("ix_catalysts_symbol", table_name="catalysts")
    op.drop_index("ix_catalysts_status_window_start", table_name="catalysts")
    op.drop_table("catalysts")
    op.execute("DELETE FROM events WHERE origin = 'finra'")
    _origins(ORIGINS_M7)
