"""M2 SEC EDGAR + deterministic risk: filings, insider_txns, fundamentals_q, capital_structure,
lockups, earnings_calendar, and the event tables (events, event_sources, event_tickers,
event_classifications). Adds the alerts.event_id -> events.id foreign key.

Revision ID: 0003_edgar
Revises: 0002_market_data
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_edgar"
down_revision = "0002_market_data"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
STRICT_NO_ROWID = {"sqlite_strict": True, "sqlite_with_rowid": False}
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"

CATEGORIES = (
    "qbi_stage_change",
    "roadmap_hit",
    "roadmap_slip",
    "logical_qubit_milestone",
    "verified_advantage",
    "revenue_quality",
    "contract_with_value",
    "m_and_a",
    "earnings_release",
    "physical_qubit_count",
    "partnership_no_value",
    "analyst_rating",
    "synthetic_benchmark",
    "listicle_or_momentum",
    "dilution",
    "insider_selling",
    "lockup_expiry",
    "short_interest_spike",
    "resource_estimate_shift",
    "pqc_deadline_change",
    "exec_departure",
    "going_concern",
    "short_report",
    "guidance_cut",
    "delisting_or_compliance",
)


def _in(values: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


def _date(table: str, col: str, nullable: bool = False) -> sa.CheckConstraint:
    expr = f"{col} GLOB {DATE_GLOB}"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return sa.CheckConstraint(expr, name=f"ck_{table}_{col}_date")


def _bool(table: str, col: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{col} IN (0, 1)", name=f"ck_{table}_{col}_bool")


def _excerpt(table: str, col: str = "excerpt") -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{col} IS NULL OR length({col}) <= 600", name=f"ck_{table}_{col}_len"
    )


def upgrade() -> None:
    op.create_table(
        "filings",
        sa.Column("accession", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("cik", sa.Text(), nullable=False),
        sa.Column("form", sa.Text(), nullable=False),
        sa.Column("filed_at", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.Text(), nullable=True),
        sa.Column("report_date", sa.Text(), nullable=True),
        sa.Column("items", sa.Text(), server_default="[]", nullable=False),
        sa.Column("primary_doc", sa.Text(), nullable=True),
        sa.Column("primary_doc_description", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("is_xbrl", sa.Integer(), server_default="0", nullable=False),
        sa.Column("parsed", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "accession GLOB '[0-9]*-[0-9][0-9]-[0-9]*'", name="ck_filings_accession_format"
        ),
        _date("filings", "filed_at"),
        sa.CheckConstraint("json_valid(items)", name="ck_filings_items_json"),
        sa.CheckConstraint("parsed IS NULL OR json_valid(parsed)", name="ck_filings_parsed_json"),
        _bool("filings", "is_xbrl"),
        sa.ForeignKeyConstraint(["symbol"], ["tickers.symbol"], name="fk_filings_symbol_tickers"),
        sa.PrimaryKeyConstraint("accession", name="pk_filings"),
        **STRICT,
    )
    op.create_index("ix_filings_symbol_filed_at", "filings", ["symbol", "filed_at"])
    op.create_index("ix_filings_form", "filings", ["form"])

    op.create_table(
        "insider_txns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("accession", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("insider_cik", sa.Text(), nullable=True),
        sa.Column("insider", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("security", sa.Text(), nullable=True),
        sa.Column("txn_date", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("acquired_disposed", sa.Text(), nullable=True),
        sa.Column("shares", sa.Integer(), nullable=True),
        sa.Column("price", sa.REAL(), nullable=True),
        sa.Column("is_10b5_1", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_derivative", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("length(code) = 1", name="ck_insider_txns_code_len"),
        sa.CheckConstraint(
            "acquired_disposed IS NULL OR acquired_disposed IN ('A','D')",
            name="ck_insider_txns_acquired_disposed",
        ),
        _date("insider_txns", "txn_date"),
        _bool("insider_txns", "is_10b5_1"),
        _bool("insider_txns", "is_derivative"),
        sa.ForeignKeyConstraint(
            ["accession"], ["filings.accession"], name="fk_insider_txns_accession_filings"
        ),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_insider_txns_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_insider_txns"),
        sa.UniqueConstraint("accession", "seq", name="uq_insider_txns_accession_seq"),
        **STRICT,
    )
    op.create_index("ix_insider_txns_symbol_txn_date", "insider_txns", ["symbol", "txn_date"])

    op.create_table(
        "fundamentals_q",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("period_end", sa.Text(), nullable=False),
        sa.Column("concept", sa.Text(), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("value_micros", sa.Integer(), nullable=True),
        sa.Column("value_int", sa.Integer(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("fy", sa.Integer(), nullable=True),
        sa.Column("fp", sa.Text(), nullable=True),
        sa.Column("form", sa.Text(), nullable=True),
        sa.Column("accession", sa.Text(), nullable=True),
        sa.Column("filed", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(value_micros IS NULL) + (value_int IS NULL) = 1", name="ck_fundamentals_q_one_value"
        ),
        sa.CheckConstraint("period_days >= 0", name="ck_fundamentals_q_period_days"),
        _date("fundamentals_q", "period_end"),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_fundamentals_q_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint(
            "symbol", "period_end", "concept", "period_days", name="pk_fundamentals_q"
        ),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "capital_structure",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("instrument", sa.Text(), nullable=False),
        sa.Column("source_accession", sa.Text(), nullable=False),
        sa.Column("amount_micros", sa.Integer(), nullable=True),
        sa.Column("shares_underlying", sa.Integer(), nullable=True),
        sa.Column("strike_micros", sa.Integer(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("concept", sa.Text(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "instrument IN ('convertible','warrant','earnout','atm','shelf')",
            name="ck_capital_structure_instrument",
        ),
        sa.CheckConstraint(
            "source IN ('xbrl','filing_text','form')", name="ck_capital_structure_source"
        ),
        _date("capital_structure", "as_of"),
        _excerpt("capital_structure"),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_capital_structure_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint(
            "symbol", "as_of", "instrument", "source_accession", name="pk_capital_structure"
        ),
        **STRICT,
    )

    op.create_table(
        "lockups",
        sa.Column("accession", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("prospectus_date", sa.Text(), nullable=False),
        sa.Column("lockup_days", sa.Integer(), nullable=False),
        sa.Column("expiry_date", sa.Text(), nullable=False),
        sa.Column("early_release_possible", sa.Integer(), server_default="0", nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.CheckConstraint("lockup_days BETWEEN 1 AND 1095", name="ck_lockups_lockup_days"),
        _date("lockups", "prospectus_date"),
        _date("lockups", "expiry_date"),
        _bool("lockups", "early_release_possible"),
        _excerpt("lockups"),
        sa.ForeignKeyConstraint(
            ["accession"], ["filings.accession"], name="fk_lockups_accession_filings"
        ),
        sa.ForeignKeyConstraint(["symbol"], ["tickers.symbol"], name="fk_lockups_symbol_tickers"),
        sa.PrimaryKeyConstraint("accession", name="pk_lockups"),
        **STRICT,
    )

    op.create_table(
        "earnings_calendar",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("date", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('scheduled','reported')", name="ck_earnings_calendar_status"
        ),
        sa.CheckConstraint("source IN ('8k_2.02','yfinance')", name="ck_earnings_calendar_source"),
        _date("earnings_calendar", "date"),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_earnings_calendar_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("symbol", "date", name="pk_earnings_calendar"),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("url_hash", sa.LargeBinary(), nullable=False),
        sa.Column("simhash", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source_domain", sa.Text(), nullable=False),
        sa.Column("trust_tier", sa.Text(), nullable=False),
        sa.Column("independent_source_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("published_at", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("accession", sa.Text(), nullable=True),
        sa.Column("injection_suspected", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quarantined", sa.Integer(), server_default="0", nullable=False),
        sa.Column("raw", sa.Text(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("trust_tier IN ('T1','T2','T3')", name="ck_events_trust_tier"),
        sa.CheckConstraint(
            "origin IN ('rss','edgar','web_search','manual')", name="ck_events_origin"
        ),
        sa.CheckConstraint(
            "independent_source_count >= 1", name="ck_events_independent_source_count"
        ),
        _excerpt("events"),
        _bool("events", "injection_suspected"),
        _bool("events", "quarantined"),
        sa.CheckConstraint("json_valid(raw)", name="ck_events_raw_json"),
        sa.ForeignKeyConstraint(
            ["accession"], ["filings.accession"], name="fk_events_accession_filings"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("url_hash", name="uq_events_url_hash"),
        **STRICT,
    )
    op.create_index("ix_events_published_at", "events", ["published_at"])
    op.create_index("ix_events_accession", "events", ["accession"])

    op.create_table(
        "event_sources",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("trust_tier", sa.Text(), nullable=False),
        sa.CheckConstraint("trust_tier IN ('T1','T2','T3')", name="ck_event_sources_trust_tier"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name="fk_event_sources_event_id_events",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", "url", name="pk_event_sources"),
        **STRICT_NO_ROWID,
    )

    op.create_table(
        "event_tickers",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name="fk_event_tickers_event_id_events",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["symbol"], ["tickers.symbol"], name="fk_event_tickers_symbol_tickers"
        ),
        sa.PrimaryKeyConstraint("event_id", "symbol", name="pk_event_tickers"),
        **STRICT_NO_ROWID,
    )
    op.create_index("ix_event_tickers_symbol", "event_tickers", ["symbol"])

    op.create_table(
        "event_classifications",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("class", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("materiality_raw", sa.Integer(), nullable=False),
        sa.Column("materiality", sa.Integer(), nullable=False),
        sa.Column("direction", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("evidence_quote", sa.Text(), nullable=True),
        sa.Column("rule_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "class IN ('SIGNAL','NOISE','RISK')", name="ck_event_classifications_class"
        ),
        sa.CheckConstraint(
            f"category IN {_in(CATEGORIES)}", name="ck_event_classifications_category"
        ),
        sa.CheckConstraint(
            "materiality_raw BETWEEN 1 AND 5", name="ck_event_classifications_materiality_raw"
        ),
        sa.CheckConstraint(
            "materiality BETWEEN 1 AND 5", name="ck_event_classifications_materiality"
        ),
        sa.CheckConstraint("direction IN (-1, 0, 1)", name="ck_event_classifications_direction"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_event_classifications_confidence"
        ),
        sa.CheckConstraint(
            "rule_id IS NOT NULL OR (model IS NOT NULL AND prompt_version IS NOT NULL)",
            name="ck_event_classifications_provenance",
        ),
        _excerpt("event_classifications", "evidence_quote"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name="fk_event_classifications_event_id_events",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_event_classifications"),
        **STRICT,
    )
    op.create_index(
        "ix_event_classifications_class_materiality",
        "event_classifications",
        ["class", "materiality"],
    )
    op.create_index(
        "ix_event_classifications_materiality_not_noise",
        "event_classifications",
        ["materiality"],
        sqlite_where=sa.text("class != 'NOISE'"),
    )

    # alerts.event_id -> events.id (the column exists since 0001). Batch rebuild keeps STRICT.
    with op.batch_alter_table("alerts", table_kwargs=STRICT) as batch:
        batch.create_foreign_key("fk_alerts_event_id_events", "events", ["event_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("alerts", table_kwargs=STRICT) as batch:
        batch.drop_constraint("fk_alerts_event_id_events", type_="foreignkey")
    for table in (
        "event_classifications",
        "event_tickers",
        "event_sources",
        "events",
        "earnings_calendar",
        "lockups",
        "capital_structure",
        "fundamentals_q",
        "insider_txns",
        "filings",
    ):
        op.drop_table(table)
