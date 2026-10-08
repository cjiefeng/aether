"""M14 adjacent industries & thesis checks: ticker type `adjacent` with `tickers.modality` and
`tickers.sector`; the universe review's `full` kind, `adjacent` track and the §6.7.1 candidate
columns; alert kind `universe_strong_candidate`; `catalysts.tags`. Every rebuild keeps STRICT via
table_kwargs.

Revision ID: 0016_adjacent_thesis
Revises: 0015_alert_noise
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_adjacent_thesis"
down_revision = "0015_alert_noise"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
TYPES_M0 = ("etf", "pure_play", "benchmark", "context")
TYPES_M14 = ("etf", "pure_play", "adjacent", "benchmark", "context")
MODALITIES = (
    "superconducting",
    "trapped_ion",
    "neutral_atom",
    "photonic",
    "annealing",
    "spin_silicon",
    "other",
)
SECTORS = (
    "pqc_cyber",
    "sensing_timing",
    "test_measurement",
    "photonics_lasers",
    "cryogenics_gases",
    "telecom_networking",
    "specialty_materials",
    "end_user",
)
REVIEW_KINDS_M12 = ("monthly", "manual")
REVIEW_KINDS_M14 = ("monthly", "manual", "full")
TRACKS_M12 = ("pure_play",)
TRACKS_M14 = ("pure_play", "adjacent")
EXPOSURES = ("high", "med", "low")
BUCKETS = ("small", "mid", "large")
ALERT_KINDS_M13 = (
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
    "digest",
)
ALERT_KINDS_M14 = (*ALERT_KINDS_M13, "universe_strong_candidate")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table("tickers", recreate="always", table_kwargs=STRICT) as batch:
        batch.add_column(sa.Column("modality", sa.Text(), nullable=True))
        batch.add_column(sa.Column("sector", sa.Text(), nullable=True))
        batch.drop_constraint("ck_tickers_type", type_="check")
        batch.create_check_constraint("ck_tickers_type", f"type IN {_in(TYPES_M14)}")
        batch.create_check_constraint(
            "ck_tickers_modality", f"modality IS NULL OR modality IN {_in(MODALITIES)}"
        )
        batch.create_check_constraint(
            "ck_tickers_sector", f"sector IS NULL OR sector IN {_in(SECTORS)}"
        )

    with op.batch_alter_table("universe_reviews", recreate="always", table_kwargs=STRICT) as b:
        b.drop_constraint("ck_universe_reviews_kind", type_="check")
        b.create_check_constraint("ck_universe_reviews_kind", f"kind IN {_in(REVIEW_KINDS_M14)}")

    with op.batch_alter_table("universe_candidates", recreate="always", table_kwargs=STRICT) as b:
        b.add_column(sa.Column("sector", sa.Text(), nullable=True))
        b.add_column(sa.Column("exposure", sa.Text(), nullable=True))
        b.add_column(sa.Column("market_cap_micros", sa.Integer(), nullable=True))
        b.add_column(sa.Column("mcap_bucket", sa.Text(), nullable=True))
        b.drop_constraint("ck_universe_candidates_track", type_="check")
        b.create_check_constraint("ck_universe_candidates_track", f"track IN {_in(TRACKS_M14)}")
        b.create_check_constraint(
            "ck_universe_candidates_sector", f"sector IS NULL OR sector IN {_in(SECTORS)}"
        )
        b.create_check_constraint(
            "ck_universe_candidates_exposure", f"exposure IS NULL OR exposure IN {_in(EXPOSURES)}"
        )
        b.create_check_constraint(
            "ck_universe_candidates_mcap_bucket",
            f"mcap_bucket IS NULL OR mcap_bucket IN {_in(BUCKETS)}",
        )
        b.create_check_constraint(
            "ck_universe_candidates_market_cap",
            "market_cap_micros IS NULL OR market_cap_micros >= 0",
        )

    with op.batch_alter_table("alerts", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_alerts_kind", type_="check")
        batch.create_check_constraint("ck_alerts_kind", f"kind IN {_in(ALERT_KINDS_M14)}")

    with op.batch_alter_table("catalysts", recreate="always", table_kwargs=STRICT) as batch:
        batch.add_column(sa.Column("tags", sa.Text(), server_default="[]", nullable=False))
        batch.create_check_constraint("ck_catalysts_tags_json", "json_valid(tags)")


def downgrade() -> None:
    with op.batch_alter_table("catalysts", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_catalysts_tags_json", type_="check")
        batch.drop_column("tags")

    op.execute("DELETE FROM alerts WHERE kind = 'universe_strong_candidate'")
    with op.batch_alter_table("alerts", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_alerts_kind", type_="check")
        batch.create_check_constraint("ck_alerts_kind", f"kind IN {_in(ALERT_KINDS_M13)}")

    op.execute("DELETE FROM universe_candidates WHERE track = 'adjacent'")
    with op.batch_alter_table("universe_candidates", recreate="always", table_kwargs=STRICT) as b:
        for ck in ("sector", "exposure", "mcap_bucket", "market_cap"):
            b.drop_constraint(f"ck_universe_candidates_{ck}", type_="check")
        b.drop_constraint("ck_universe_candidates_track", type_="check")
        b.create_check_constraint("ck_universe_candidates_track", f"track IN {_in(TRACKS_M12)}")
        for col in ("mcap_bucket", "market_cap_micros", "exposure", "sector"):
            b.drop_column(col)

    op.execute("DELETE FROM universe_reviews WHERE kind = 'full'")
    with op.batch_alter_table("universe_reviews", recreate="always", table_kwargs=STRICT) as b:
        b.drop_constraint("ck_universe_reviews_kind", type_="check")
        b.create_check_constraint("ck_universe_reviews_kind", f"kind IN {_in(REVIEW_KINDS_M12)}")

    # Adjacent tickers are referenced by prices, filings, events…; deactivate rather than delete.
    op.execute("UPDATE tickers SET type = 'context', active = 0 WHERE type = 'adjacent'")
    with op.batch_alter_table("tickers", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_tickers_sector", type_="check")
        batch.drop_constraint("ck_tickers_modality", type_="check")
        batch.drop_constraint("ck_tickers_type", type_="check")
        batch.create_check_constraint("ck_tickers_type", f"type IN {_in(TYPES_M0)}")
        batch.drop_column("sector")
        batch.drop_column("modality")
