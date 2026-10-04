"""Table definitions (SQLAlchemy Core). All tables are STRICT (spec §2.1).

STRICT tables accept only INTEGER/REAL/TEXT/BLOB/ANY, so columns use only `Integer`, `REAL`,
`Text`, `LargeBinary` and `Micros`. Never use String/Boolean/Float/DateTime here: they render
VARCHAR/BOOLEAN/FLOAT/DATETIME and the CREATE TABLE fails.

Each milestone adds its own tables plus a migration (M0: infra, M1: market data, M2: EDGAR +
events, M3: alerts outbox). Keep this file and `migrations/versions/*` in sync (a test compares
them).
"""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from aether.db.types import Micros

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING)


def _json_ck(col: str, nullable: bool = False) -> CheckConstraint:
    expr = f"json_valid({col})"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"{col}_json")


def _bool_ck(col: str) -> CheckConstraint:
    return CheckConstraint(f"{col} IN (0, 1)", name=f"{col}_bool")


tickers = Table(
    "tickers",
    metadata,
    Column("symbol", Text, primary_key=True),
    Column("name", Text),
    Column("type", Text, nullable=False),
    Column("cik", Text),
    Column("active", Integer, nullable=False, server_default="1"),
    CheckConstraint("type IN ('etf','pure_play','benchmark','context')", name="type"),
    _bool_ck("active"),
    sqlite_strict=True,
)

facts = Table(
    "facts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("claim", Text, nullable=False),
    Column("source_urls", Text, nullable=False),  # JSON array of http(s) URLs
    Column("retrieved_at", Text),
    Column("status", Text, nullable=False),
    Column("notes", Text),
    Column("open_question", Text),  # M3
    Column("synced_at", Text, nullable=False),
    CheckConstraint("status IN ('unverified','verified_by_claude','signed_off')", name="status"),
    _json_ck("source_urls"),
    sqlite_strict=True,
)

commands = Table(
    "commands",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("args", Text, nullable=False, server_default="{}"),
    Column("requested_at", Text, nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("processed_at", Text),
    Column("result", Text),
    CheckConstraint("length(kind) BETWEEN 1 AND 64", name="kind_len"),
    CheckConstraint("status IN ('pending','running','done','failed','rejected')", name="status"),
    _json_ck("args"),
    _json_ck("result", nullable=True),
    Index(None, "requested_at"),
    Index(None, "status", "id"),
    sqlite_strict=True,
)

job_runs = Table(
    "job_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("job", Text, nullable=False),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("status", Text, nullable=False),
    Column("rows_written", Integer),
    Column("provider", Text),
    Column("error", Text),
    CheckConstraint("status IN ('running','ok','failed')", name="status"),
    Index(None, "job", "started_at"),
    sqlite_strict=True,
)

llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("purpose", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cache_read_tokens", Integer, nullable=False, server_default="0"),
    Column("web_searches", Integer, nullable=False, server_default="0"),
    Column("cost_micros", Micros, nullable=False, server_default="0"),
    Column("created_at", Text, nullable=False),
    Index(None, "created_at"),
    sqlite_strict=True,
)

ALERT_KINDS = (
    "risk_event",
    "insider_cluster",
    "lockup_reminder",
    "earnings_reminder",
    "job_failing",
    "job_recovered",
    "test",
)
ALERT_STATUSES = ("pending", "sent", "failed", "expired", "dashboard_only")

# M3 (0004) rebuilt this table: an outbox. Rows are inserted once per dedupe_key; the worker sends
# pending telegram rows outside any write transaction and records the outcome.
alerts = Table(
    "alerts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("event_id", Integer, ForeignKey("events.id")),
    Column("kind", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("text", Text, nullable=False),  # plain text, exactly what is (or would be) sent
    Column("created_at", Text, nullable=False),
    Column("sent_at", Text),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("payload", Text, nullable=False, server_default="{}"),
    Column("dedupe_key", Text, nullable=False, unique=True),
    CheckConstraint("channel IN ('telegram','dashboard')", name="channel"),
    CheckConstraint("kind IN (" + ",".join(f"'{k}'" for k in ALERT_KINDS) + ")", name="kind"),
    CheckConstraint(
        "status IN (" + ",".join(f"'{k}'" for k in ALERT_STATUSES) + ")", name="status"
    ),
    CheckConstraint("length(text) BETWEEN 1 AND 4096", name="text_len"),
    CheckConstraint("attempts >= 0", name="attempts"),
    _json_ck("payload"),
    Index(None, "status", "id"),
    Index(None, "created_at"),
    sqlite_strict=True,
)

# --------------------------------------------------------------------------- M1: market data

PRICE_PROVIDERS = ("yfinance", "massive", "synthetic")

prices_daily = Table(
    "prices_daily",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("d", Text, nullable=False),  # trading day, YYYY-MM-DD (US/Eastern session date)
    Column("o", REAL, nullable=False),
    Column("h", REAL, nullable=False),
    Column("l", REAL, nullable=False),
    Column("c", REAL, nullable=False),
    Column("volume", Integer, nullable=False),
    Column("provider", Text, nullable=False),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("symbol", "d"),
    CheckConstraint(
        "provider IN (" + ",".join(f"'{p}'" for p in PRICE_PROVIDERS) + ")", name="provider"
    ),
    CheckConstraint("o > 0 AND h > 0 AND l > 0 AND c > 0 AND h >= l", name="ohlc"),
    CheckConstraint("volume >= 0", name="volume"),
    CheckConstraint("d GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", name="d_format"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

qtum_holdings = Table(
    "qtum_holdings",
    metadata,
    Column("snapshot_date", Text, nullable=False),  # the issuer's "data as of" date
    Column("holding_symbol", Text, nullable=False),  # issuer's ticker text, e.g. "3443 TT"
    Column("name", Text),
    Column("cusip", Text),
    Column("weight", REAL, nullable=False),  # percent of fund, e.g. 1.09
    Column("shares", Integer),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("snapshot_date", "holding_symbol"),
    CheckConstraint("weight >= -100 AND weight <= 100", name="weight"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# --------------------------------------------------------------------------- M2: SEC EDGAR

DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def _date_ck(col: str, nullable: bool = False) -> CheckConstraint:
    expr = f"{col} GLOB {DATE_GLOB}"
    if nullable:
        expr = f"{col} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"{col}_date")


def _excerpt_ck(col: str = "excerpt") -> CheckConstraint:
    # S6: excerpts only (~500 chars), never full bodies.
    return CheckConstraint(f"{col} IS NULL OR length({col}) <= 600", name=f"{col}_len")


filings = Table(
    "filings",
    metadata,
    Column("accession", Text, primary_key=True),  # 0001234567-26-000123
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("cik", Text, nullable=False),
    Column("form", Text, nullable=False),
    Column("filed_at", Text, nullable=False),  # YYYY-MM-DD
    Column("accepted_at", Text),  # SEC acceptance timestamp, UTC ISO
    Column("report_date", Text),
    Column("items", Text, nullable=False, server_default="[]"),  # JSON array, 8-K items
    Column("primary_doc", Text),
    Column("primary_doc_description", Text),
    Column("url", Text, nullable=False),
    Column("is_xbrl", Integer, nullable=False, server_default="0"),
    Column("parsed", Text),  # JSON: extractor results; NULL = document not processed (yet)
    Column("fetched_at", Text, nullable=False),
    CheckConstraint("accession GLOB '[0-9]*-[0-9][0-9]-[0-9]*'", name="accession_format"),
    _date_ck("filed_at"),
    _json_ck("items"),
    _json_ck("parsed", nullable=True),
    _bool_ck("is_xbrl"),
    Index(None, "symbol", "filed_at"),
    Index(None, "form"),
    sqlite_strict=True,
)

insider_txns = Table(
    "insider_txns",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("accession", Text, ForeignKey("filings.accession"), nullable=False),
    Column("seq", Integer, nullable=False),  # order within the Form 4
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("insider_cik", Text),
    Column("insider", Text, nullable=False),
    Column("role", Text),
    Column("security", Text),
    Column("txn_date", Text, nullable=False),
    Column("code", Text, nullable=False),  # SEC transaction code: S, P, M, F, A, G, ...
    Column("acquired_disposed", Text),
    Column("shares", Integer),  # rounded to whole shares
    Column("price", REAL),
    Column("is_10b5_1", Integer, nullable=False, server_default="0"),
    Column("is_derivative", Integer, nullable=False, server_default="0"),
    UniqueConstraint("accession", "seq"),
    CheckConstraint("length(code) = 1", name="code_len"),
    CheckConstraint(
        "acquired_disposed IS NULL OR acquired_disposed IN ('A','D')", name="acquired_disposed"
    ),
    _date_ck("txn_date"),
    _bool_ck("is_10b5_1"),
    _bool_ck("is_derivative"),
    Index(None, "symbol", "txn_date"),
    sqlite_strict=True,
)

fundamentals_q = Table(
    "fundamentals_q",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("period_end", Text, nullable=False),
    Column("concept", Text, nullable=False),  # "us-gaap:Revenues", "dei:EntityCommon..."
    Column("period_days", Integer, nullable=False),  # 0 = instant; ~91 = quarter; ~365 = FY
    Column("value_micros", Micros),  # USD amounts
    Column("value_int", Integer),  # share counts
    Column("unit", Text, nullable=False),
    Column("fy", Integer),
    Column("fp", Text),
    Column("form", Text),
    Column("accession", Text),
    Column("filed", Text),
    PrimaryKeyConstraint("symbol", "period_end", "concept", "period_days"),
    CheckConstraint("(value_micros IS NULL) + (value_int IS NULL) = 1", name="one_value"),
    CheckConstraint("period_days >= 0", name="period_days"),
    _date_ck("period_end"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

CAPITAL_INSTRUMENTS = ("convertible", "warrant", "earnout", "atm", "shelf")

capital_structure = Table(
    "capital_structure",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("as_of", Text, nullable=False),
    Column("instrument", Text, nullable=False),
    Column("source_accession", Text, nullable=False),
    Column("amount_micros", Micros),
    Column("shares_underlying", Integer),
    Column("strike_micros", Micros),
    Column("source", Text, nullable=False),
    Column("concept", Text),  # XBRL concept when source = 'xbrl'
    Column("excerpt", Text),
    PrimaryKeyConstraint("symbol", "as_of", "instrument", "source_accession"),
    CheckConstraint(
        "instrument IN (" + ",".join(f"'{i}'" for i in CAPITAL_INSTRUMENTS) + ")",
        name="instrument",
    ),
    CheckConstraint("source IN ('xbrl','filing_text','form')", name="source"),
    _date_ck("as_of"),
    _excerpt_ck(),
    sqlite_strict=True,
)

lockups = Table(
    "lockups",
    metadata,
    Column("accession", Text, ForeignKey("filings.accession"), primary_key=True),
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("prospectus_date", Text, nullable=False),
    Column("lockup_days", Integer, nullable=False),
    Column("expiry_date", Text, nullable=False),
    Column("early_release_possible", Integer, nullable=False, server_default="0"),
    Column("excerpt", Text, nullable=False),
    CheckConstraint("lockup_days BETWEEN 1 AND 1095", name="lockup_days"),
    _date_ck("prospectus_date"),
    _date_ck("expiry_date"),
    _bool_ck("early_release_possible"),
    _excerpt_ck(),
    sqlite_strict=True,
)

earnings_calendar = Table(
    "earnings_calendar",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("date", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("source_url", Text),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("symbol", "date"),
    CheckConstraint("status IN ('scheduled','reported')", name="status"),
    CheckConstraint("source IN ('8k_2.02','yfinance')", name="source"),
    _date_ck("date"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# --------------------------------------------------------------------------- events (M2 → M7)
# EDGAR filings are the first event origin (deterministic RISK rules, spec §5.2 step 1). M6 adds
# RSS/web-search events and M7 LLM classifications to the same tables.

EVENT_CLASSES = ("SIGNAL", "NOISE", "RISK")
EVENT_CATEGORIES = (
    # SIGNAL
    "qbi_stage_change",
    "roadmap_hit",
    "roadmap_slip",
    "logical_qubit_milestone",
    "verified_advantage",
    "revenue_quality",
    "contract_with_value",
    "m_and_a",
    "earnings_release",
    # NOISE
    "physical_qubit_count",
    "partnership_no_value",
    "analyst_rating",
    "synthetic_benchmark",
    "listicle_or_momentum",
    # RISK
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
TRUST_TIERS = ("T1", "T2", "T3")


def _in_ck(col: str, values: tuple[str, ...], name: str | None = None) -> CheckConstraint:
    return CheckConstraint(
        f"{col} IN (" + ",".join(f"'{v}'" for v in values) + ")", name=name or col
    )


events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("url_hash", LargeBinary, nullable=False, unique=True),  # sha256(canonical url)
    Column("simhash", Integer),  # signed 64-bit (db.types.u64_to_i64)
    Column("title", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("source_domain", Text, nullable=False),
    Column("trust_tier", Text, nullable=False),
    Column("independent_source_count", Integer, nullable=False, server_default="1"),
    Column("published_at", Text, nullable=False),  # UTC ISO
    Column("excerpt", Text),
    Column("origin", Text, nullable=False),
    Column("accession", Text, ForeignKey("filings.accession")),  # origin = 'edgar'
    Column("injection_suspected", Integer, nullable=False, server_default="0"),
    Column("quarantined", Integer, nullable=False, server_default="0"),
    Column("raw", Text, nullable=False, server_default="{}"),
    Column("created_at", Text, nullable=False),
    _in_ck("trust_tier", TRUST_TIERS),
    _in_ck("origin", ("rss", "edgar", "web_search", "manual")),
    CheckConstraint("independent_source_count >= 1", name="independent_source_count"),
    _excerpt_ck(),
    _bool_ck("injection_suspected"),
    _bool_ck("quarantined"),
    _json_ck("raw"),
    Index(None, "published_at"),
    Index(None, "accession"),
    sqlite_strict=True,
)

event_sources = Table(
    "event_sources",
    metadata,
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
    Column("url", Text, nullable=False),
    Column("domain", Text, nullable=False),
    Column("trust_tier", Text, nullable=False),
    PrimaryKeyConstraint("event_id", "url"),
    _in_ck("trust_tier", TRUST_TIERS),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

event_tickers = Table(
    "event_tickers",
    metadata,
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    PrimaryKeyConstraint("event_id", "symbol"),
    Index(None, "symbol"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

event_classifications = Table(
    "event_classifications",
    metadata,
    Column(
        "event_id",
        Integer,
        ForeignKey("events.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("class", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("materiality_raw", Integer, nullable=False),
    Column("materiality", Integer, nullable=False),  # after trust-tier caps
    Column("direction", Integer, nullable=False),
    Column("confidence", REAL, nullable=False),
    Column("rationale", Text),
    Column("evidence_quote", Text),
    Column("rule_id", Text),
    Column("model", Text),
    Column("prompt_version", Text),
    Column("created_at", Text, nullable=False),
    _in_ck("class", EVENT_CLASSES, name="class"),
    _in_ck("category", EVENT_CATEGORIES),
    CheckConstraint("materiality_raw BETWEEN 1 AND 5", name="materiality_raw"),
    CheckConstraint("materiality BETWEEN 1 AND 5", name="materiality"),
    CheckConstraint("direction IN (-1, 0, 1)", name="direction"),
    CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
    CheckConstraint(
        "rule_id IS NOT NULL OR (model IS NOT NULL AND prompt_version IS NOT NULL)",
        name="provenance",
    ),
    _excerpt_ck("evidence_quote"),
    Index(None, "class", "materiality"),
    Index(
        "ix_event_classifications_materiality_not_noise",
        "materiality",
        sqlite_where=text("class != 'NOISE'"),
    ),
    sqlite_strict=True,
)
