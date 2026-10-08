"""Table definitions (SQLAlchemy Core). All tables are STRICT (spec §2.1).

STRICT tables accept only INTEGER/REAL/TEXT/BLOB/ANY, so columns use only `Integer`, `REAL`,
`Text`, `LargeBinary` and `Micros`. Never use String/Boolean/Float/DateTime here: they render
VARCHAR/BOOLEAN/FLOAT/DATETIME and the CREATE TABLE fails.

Each milestone adds its own tables plus a migration (M0: infra, M1: market data, M2: EDGAR +
events, M3: alerts outbox, M4: dividends + backtests, M5: holdings + rebalance, M6: news +
research, M7: classifier state + eval runs, M8: catalysts + short interest, M9: scores, M10:
conclusions + track record + briefs, M11: escalations, M12: universe review). Keep this file and
`migrations/versions/*` in sync (a test compares them).
"""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
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
    # M6: one row per call attempt (prompts are never stored). `budget_refused` rows cost 0 and
    # record that the soft budget guard stopped a call before any request was sent.
    Column("status", Text, nullable=False, server_default="ok"),
    Column("batch", Integer, nullable=False, server_default="0"),
    Column("cache_write_tokens", Integer, nullable=False, server_default="0"),
    Column("request_id", Text),
    Column("research_run_id", Integer, ForeignKey("research_runs.id")),
    Column("error", Text),
    CheckConstraint("status IN ('ok','error','budget_refused')", name="status"),
    CheckConstraint("batch IN (0, 1)", name="batch_bool"),
    CheckConstraint(
        "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
        "AND cache_write_tokens >= 0 AND web_searches >= 0 AND cost_micros >= 0",
        name="non_negative",
    ),
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
    "off_cycle_review",  # M5
    "review_pack",  # M5
    "llm_budget",  # M6
    "weekly_brief",  # M10
    "escalation",  # M11
    "escalation_result",  # M11
    "universe_review",  # M12
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
# M8 adds `finra` (short-interest spike rule events from FINRA's bi-weekly short-interest files).
EVENT_ORIGINS = ("rss", "edgar", "web_search", "manual", "finra")


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
    _in_ck("origin", EVENT_ORIGINS),
    CheckConstraint("independent_source_count >= 1", name="independent_source_count"),
    _excerpt_ck(),
    _bool_ck("injection_suspected"),
    _bool_ck("quarantined"),
    _json_ck("raw"),
    Index(None, "published_at"),
    Index(None, "accession"),
    Index(None, "simhash"),  # M6
    Index(None, "source_domain", "published_at"),  # M6
    sqlite_strict=True,
)

event_sources = Table(
    "event_sources",
    metadata,
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
    Column("url", Text, nullable=False),
    Column("domain", Text, nullable=False),
    Column("trust_tier", Text, nullable=False),
    # M6: per-source detail for news/research merges. `syndicated` copies (wire/mirror domains or
    # the same body text) don't count towards events.independent_source_count.
    Column("title", Text),
    Column("published_at", Text),
    Column("excerpt", Text),
    Column("simhash", Integer),  # title simhash, signed 64-bit
    Column("excerpt_simhash", Integer),
    Column("syndicated", Integer, nullable=False, server_default="0"),
    Column("origin", Text),
    Column("added_at", Text),
    PrimaryKeyConstraint("event_id", "url"),
    _in_ck("trust_tier", TRUST_TIERS),
    _excerpt_ck(),
    _bool_ck("syndicated"),
    CheckConstraint(
        "origin IS NULL OR origin IN (" + ",".join(f"'{o}'" for o in EVENT_ORIGINS) + ")",
        name="origin",
    ),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

event_tickers = Table(
    "event_tickers",
    metadata,
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    # M7: the classifier's direction for this ticker (spec §5: per affected ticker). NULL until
    # classified; EDGAR rule events get the rule's direction.
    Column("direction", Integer),
    PrimaryKeyConstraint("event_id", "symbol"),
    CheckConstraint("direction IS NULL OR direction IN (-1, 0, 1)", name="direction"),
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

# --------------------------------------------------------------------------- M4: portfolio

PROFILES = ("safe", "medium", "aggressive")
DIVIDEND_PROVIDERS = ("yfinance", "massive", "synthetic")

# Cash dividends per share, split-adjusted like prices_daily. Total return is computed in code
# from prices_daily + this table, so the providers' own adjusted-close conventions never mix.
dividends = Table(
    "dividends",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("ex_date", Text, nullable=False),
    Column("amount_micros", Micros, nullable=False),
    Column("currency", Text, nullable=False, server_default="USD"),
    Column("provider", Text, nullable=False),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("symbol", "ex_date"),
    CheckConstraint("amount_micros > 0", name="amount"),
    CheckConstraint("currency = 'USD'", name="currency"),
    _in_ck("provider", DIVIDEND_PROVIDERS),
    _date_ck("ex_date"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# One row per distinct (as_of, input_hash): a re-run on identical inputs writes nothing.
strategy_runs = Table(
    "strategy_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("as_of", Text, nullable=False),  # last QTUM session in the inputs
    Column("input_hash", LargeBinary, nullable=False),
    Column("config", Text, nullable=False),
    Column("summary", Text, nullable=False),  # selections, caveats, OOS window
    Column("created_at", Text, nullable=False),
    UniqueConstraint("as_of", "input_hash"),
    _date_ck("as_of"),
    CheckConstraint("length(input_hash) = 32", name="input_hash_len"),
    _json_ck("config"),
    _json_ck("summary"),
    sqlite_strict=True,
)

strategy_metrics = Table(
    "strategy_metrics",
    metadata,
    Column("run_id", Integer, ForeignKey("strategy_runs.id", ondelete="CASCADE"), nullable=False),
    Column("strategy_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("profile", Text),
    Column("family", Text),
    Column("qtum_weight", REAL),
    Column("metrics", Text, nullable=False),
    Column("qualifies", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "strategy_id"),
    _in_ck("kind", ("candidate", "benchmark")),
    CheckConstraint(
        "profile IS NULL OR profile IN (" + ",".join(f"'{p}'" for p in PROFILES) + ")",
        name="profile",
    ),
    CheckConstraint(
        "(kind = 'benchmark') = (profile IS NULL AND family IS NULL AND qtum_weight IS NULL)",
        name="kind_fields",
    ),
    _json_ck("metrics"),
    _json_ck("qualifies"),
    sqlite_strict=True,
)

# Current target weights per strategy (weights for the session after as_of). M5 reads these.
strategy_weights = Table(
    "strategy_weights",
    metadata,
    Column("run_id", Integer, nullable=False),
    Column("strategy_id", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("weight", REAL, nullable=False),
    PrimaryKeyConstraint("run_id", "strategy_id", "symbol"),
    ForeignKeyConstraint(
        ["run_id", "strategy_id"],
        ["strategy_metrics.run_id", "strategy_metrics.strategy_id"],
        ondelete="CASCADE",
    ),
    CheckConstraint("weight >= 0 AND weight <= 1", name="weight"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# Equity curves for the page (recommended strategies + benchmarks). Pruned to recent runs.
strategy_curves = Table(
    "strategy_curves",
    metadata,
    Column("run_id", Integer, ForeignKey("strategy_runs.id", ondelete="CASCADE"), nullable=False),
    Column("series_id", Text, nullable=False),
    Column("points", Text, nullable=False),  # JSON [[YYYY-MM-DD, level], ...]
    PrimaryKeyConstraint("run_id", "series_id"),
    _json_ck("points"),
    sqlite_strict=True,
)

# --------------------------------------------------------------------------- M5: holdings

HOLDING_SOURCES = ("manual", "tiger")
CASH = "$CASH"  # reserved holdings row: USD cash in shares_micros
PORTFOLIO_SETTING_KEYS = (
    "selected_profile",
    "whole_shares",
    "new_cash_only",
    "holdings_source",
    "tiger_sync",
    "positions_imported",
)

# The owner's sleeve: strategy-universe symbols plus the cash row. Written only by the worker
# (update_holdings / sync_holdings commands, one-time positions.yaml import).
holdings = Table(
    "holdings",
    metadata,
    Column("symbol", Text, primary_key=True),
    Column("shares_micros", Micros, nullable=False),
    Column("cost_basis_micros", Micros),  # average cost per share, USD
    Column("source", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint("shares_micros >= 0", name="shares"),
    CheckConstraint("cost_basis_micros IS NULL OR cost_basis_micros >= 0", name="cost_basis"),
    CheckConstraint(
        f"symbol != '{CASH}' OR (cost_basis_micros IS NULL AND source = 'manual')", name="cash"
    ),
    _in_ck("source", HOLDING_SOURCES),
    sqlite_strict=True,
)

holdings_history = Table(
    "holdings_history",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("command_id", Integer, ForeignKey("commands.id")),
    Column("source", Text, nullable=False),
    Column("before", Text, nullable=False),
    Column("after", Text, nullable=False),
    Column("applied_at", Text, nullable=False),
    _in_ck("source", ("manual", "tiger", "import")),
    _json_ck("before"),
    _json_ck("after"),
    Index(None, "applied_at"),
    sqlite_strict=True,
)

portfolio_settings = Table(
    "portfolio_settings",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    _in_ck("key", PORTFOLIO_SETTING_KEYS),
    _json_ck("value"),
    sqlite_strict=True,
)

# `config_change` (issue #13): republished because `strategies.yaml` changed the backtest.
TARGET_TRIGGERS = ("monthly", "off_cycle", "config_change")

# Published monthly targets per profile (spec §6.6, §6.6.1): base weights (the selected sleeve
# method) after the research overlay, with each name's adjustment chain. `as_of` is the publish
# date (SGT); a second publish on the same day replaces the row.
profile_targets = Table(
    "profile_targets",
    metadata,
    Column("profile", Text, nullable=False),
    Column("as_of", Text, nullable=False),
    Column("published_at", Text, nullable=False),
    Column("prices_as_of", Text, nullable=False),  # last session of the strategy run used
    Column("strategy_run_id", Integer, ForeignKey("strategy_runs.id", ondelete="SET NULL")),
    Column("strategy_id", Text),
    Column("base_weights", Text, nullable=False),
    Column("published_weights", Text, nullable=False),
    Column("adjustments", Text, nullable=False),
    Column("trigger", Text, nullable=False),
    Column("trigger_event_id", Integer, ForeignKey("events.id")),
    Column("input_hash", LargeBinary, nullable=False),
    PrimaryKeyConstraint("profile", "as_of"),
    _in_ck("profile", PROFILES),
    _in_ck("trigger", TARGET_TRIGGERS),
    _date_ck("as_of"),
    _date_ck("prices_as_of"),
    _json_ck("base_weights"),
    _json_ck("published_weights"),
    _json_ck("adjustments"),
    CheckConstraint("length(input_hash) = 32", name="input_hash_len"),
    sqlite_strict=True,
)

rebalance_plans = Table(
    "rebalance_plans",
    metadata,
    Column("profile", Text, nullable=False),
    Column("as_of", Text, nullable=False),
    Column("input_hash", LargeBinary, nullable=False),
    Column("plan", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    PrimaryKeyConstraint("profile", "as_of"),
    _in_ck("profile", PROFILES),
    _date_ck("as_of"),
    CheckConstraint("length(input_hash) = 32", name="input_hash_len"),
    _json_ck("plan"),
    sqlite_strict=True,
)

FX_PROVIDERS = ("yfinance", "ecb", "synthetic")

# USD/SGD reference rate: reporting only (spec §1.4, §6.6). Never used in targets or trades.
fx_rates = Table(
    "fx_rates",
    metadata,
    Column("pair", Text, nullable=False),
    Column("d", Text, nullable=False),
    Column("rate", REAL, nullable=False),  # SGD per 1 USD
    Column("provider", Text, nullable=False),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("pair", "d"),
    CheckConstraint("pair = 'USDSGD'", name="pair"),
    CheckConstraint("rate > 0", name="rate"),
    _in_ck("provider", FX_PROVIDERS),
    _date_ck("d"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

OPTIONS_PROVIDERS = ("yfinance", "synthetic")

# Daily options summary metrics (spec §6.8). Research only: never sizing or trades.
options_snapshots = Table(
    "options_snapshots",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("d", Text, nullable=False),
    Column("metrics", Text, nullable=False),
    Column("quality", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("symbol", "d"),
    _in_ck("provider", OPTIONS_PROVIDERS),
    _date_ck("d"),
    _json_ck("metrics"),
    _json_ck("quality"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# Monthly review pack (spec §6.9): the owner's decision document. `as_of` is the publish date.
review_packs = Table(
    "review_packs",
    metadata,
    Column("as_of", Text, primary_key=True),
    Column("month", Text, nullable=False),  # YYYY-MM
    Column("payload", Text, nullable=False),
    Column("telegram_text", Text),
    Column("status", Text, nullable=False),
    Column("error", Text),
    Column("created_at", Text, nullable=False),
    _in_ck("status", ("done", "failed")),
    _date_ck("as_of"),
    CheckConstraint("month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'", name="month"),
    CheckConstraint("telegram_text IS NULL OR length(telegram_text) <= 4096", name="telegram_len"),
    _json_ck("payload"),
    Index(None, "month"),
    sqlite_strict=True,
)

# --------------------------------------------------------------------------- M6: news & research

# Conditional-GET state per RSS feed (config/sources.yaml `feeds`).
feed_state = Table(
    "feed_state",
    metadata,
    Column("feed_id", Text, primary_key=True),
    Column("url", Text, nullable=False),
    Column("etag", Text),
    Column("last_modified", Text),
    Column("last_fetched_at", Text),
    Column("last_status", Integer),  # HTTP status, or NULL if the request never completed
    Column("last_error", Text),
    Column("items_seen", Integer, nullable=False, server_default="0"),
    Column("items_kept", Integer, nullable=False, server_default="0"),
    sqlite_strict=True,
)

RESEARCH_KINDS = ("sweep", "backfill", "verify")  # verify: M11 escalation
RESEARCH_STATUSES = ("running", "submitted", "done", "failed", "budget_refused")

# Claude web-search research runs (the only tool-enabled LLM calls, spec S1). One row per
# (ticker, window). Backfill rows share a Message Batch (`batch_id`); `custom_id` keys results.
research_runs = Table(
    "research_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("window_start", Text, nullable=False),
    Column("window_end", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("batch_id", Text),
    Column("custom_id", Text, unique=True),
    Column("items_found", Integer, nullable=False, server_default="0"),
    Column("events_new", Integer, nullable=False, server_default="0"),
    Column("cost_micros", Micros, nullable=False, server_default="0"),
    Column("payload", Text, nullable=False, server_default="{}"),  # audit only; never an event
    Column("error", Text),
    Column("created_at", Text, nullable=False),
    Column("finished_at", Text),
    _in_ck("kind", RESEARCH_KINDS),
    _in_ck("status", RESEARCH_STATUSES),
    _date_ck("window_start"),
    _date_ck("window_end"),
    CheckConstraint("window_start <= window_end", name="window"),
    _json_ck("payload"),
    Index(None, "kind", "status"),
    Index(None, "batch_id"),
    sqlite_strict=True,
)


# --------------------------------------------------------------------------- classifier (M7)
# Per-event classifier bookkeeping. `retry`: an attempt failed (invalid answer or API error) and
# the item is picked up again; `failed`: max attempts reached, shown on the Feed; `batched`: in a
# pending Message Batch (backlog); `done`: classified by the model. Rule hits get no state row.
CLASSIFY_STATUSES = ("retry", "batched", "failed", "done")

classify_state = Table(
    "classify_state",
    metadata,
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("batch_id", Text),
    Column("custom_id", Text, unique=True),
    Column("updated_at", Text, nullable=False),
    _in_ck("status", CLASSIFY_STATUSES),
    CheckConstraint("attempts >= 0", name="attempts"),
    Index(None, "status"),
    Index(None, "batch_id"),
    sqlite_strict=True,
)

# One row per `make eval` run (spec §5.3: store the eval result per prompt version).
eval_runs = Table(
    "eval_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("prompt_version", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("n_items", Integer, nullable=False),
    Column("provisional", Integer, nullable=False),  # 1 while any label isn't owner-reviewed
    Column("passed", Integer, nullable=False),
    Column("metrics", Text, nullable=False),
    _bool_ck("provisional"),
    _bool_ck("passed"),
    _json_ck("metrics"),
    Index(None, "prompt_version"),
    sqlite_strict=True,
)


# --------------------------------------------------------------------------- M8: catalysts

CATALYST_ORIGINS = ("seed", "earnings", "lockup")
CATALYST_KINDS = ("roadmap", "program", "earnings", "lockup", "regulatory")
CATALYST_STATUSES = ("upcoming", "hit", "slipped", "cancelled")
CATALYST_RESOLUTIONS = ("event", "date", "window_passed", "owner", "rescheduled")

# Dated catalysts (spec §1 item 3, §6.1 "catalyst position"). `key` is the natural key per origin:
# `seed:<id>` (config/catalysts_seed.yaml), `earnings:<SYM>:<date>` (earnings_calendar),
# `lockup:<accession>` (lockups). Resolution is deterministic (catalysts/resolve.py) or the owner's.
catalysts = Table(
    "catalysts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("key", Text, nullable=False, unique=True),
    Column("origin", Text, nullable=False),
    Column("symbol", Text, ForeignKey("tickers.symbol")),  # NULL = theme-wide
    Column("title", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("window_start", Text, nullable=False),
    Column("window_end", Text),  # NULL = no stated end (never auto-slips)
    Column("status", Text, nullable=False, server_default="upcoming"),
    Column("fact_id", Text, ForeignKey("facts.id")),
    Column("source_url", Text),
    Column("keywords", Text, nullable=False, server_default="[]"),
    Column("resolve_categories", Text, nullable=False, server_default="[]"),
    Column("resolved_by_event_id", Integer, ForeignKey("events.id", ondelete="SET NULL")),
    Column("resolution", Text),
    Column("resolved_at", Text),
    Column("note", Text),
    Column("updated_at", Text, nullable=False),
    _in_ck("origin", CATALYST_ORIGINS),
    _in_ck("kind", CATALYST_KINDS),
    _in_ck("status", CATALYST_STATUSES),
    CheckConstraint(
        "resolution IS NULL OR resolution IN ("
        + ",".join(f"'{r}'" for r in CATALYST_RESOLUTIONS)
        + ")",
        name="resolution",
    ),
    CheckConstraint("(status = 'upcoming') = (resolution IS NULL)", name="resolved"),
    _date_ck("window_start"),
    _date_ck("window_end", nullable=True),
    CheckConstraint("window_end IS NULL OR window_start <= window_end", name="window"),
    CheckConstraint("note IS NULL OR length(note) <= 200", name="note_len"),
    _json_ck("keywords"),
    _json_ck("resolve_categories"),
    Index(None, "status", "window_start"),
    Index(None, "symbol"),
    sqlite_strict=True,
)

# FINRA bi-weekly short interest (spec §4, §6.1). `pct_shares_out` = short shares ÷ shares
# outstanding (XBRL dei:EntityCommonStockSharesOutstanding): FINRA publishes no float figure.
short_interest = Table(
    "short_interest",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("settlement_date", Text, nullable=False),
    Column("short_shares", Integer, nullable=False),
    Column("prev_short_shares", Integer),
    Column("avg_daily_volume", Integer),
    Column("days_to_cover", REAL),
    Column("shares_out", Integer),
    Column("shares_out_as_of", Text),
    Column("pct_shares_out", REAL),
    Column("source", Text, nullable=False, server_default="finra"),
    Column("source_url", Text, nullable=False),
    Column("fetched_at", Text, nullable=False),
    PrimaryKeyConstraint("symbol", "settlement_date"),
    CheckConstraint("short_shares >= 0", name="short_shares"),
    CheckConstraint("shares_out IS NULL OR shares_out > 0", name="shares_out"),
    _in_ck("source", ("finra", "synthetic")),
    _date_ck("settlement_date"),
    _date_ck("shares_out_as_of", nullable=True),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# One row per FINRA file fetched, so each file is downloaded once.
short_interest_files = Table(
    "short_interest_files",
    metadata,
    Column("settlement_date", Text, primary_key=True),
    Column("url", Text, nullable=False),
    Column("rows", Integer, nullable=False),
    Column("fetched_at", Text, nullable=False),
    _date_ck("settlement_date"),
    sqlite_strict=True,
)

# --------------------------------------------------------------------------- M9: scores

# One row per symbol: when companyfacts was last fetched and with which parser version, so a
# parser change (new concepts, YTD durations) refetches every symbol once (M9).
xbrl_fetches = Table(
    "xbrl_fetches",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), primary_key=True),
    Column("parser_version", Text, nullable=False),
    Column("fetched_at", Text, nullable=False),
    sqlite_strict=True,
)

# Daily deterministic scorecard per ticker (spec §6.1). `components` holds each component's raw
# metrics, score in [-1, 1] (or null with a reason) and weight; `total` is in [-100, 100] over the
# components that have data (`coverage` = their share of the configured weight).
scorecards = Table(
    "scorecards",
    metadata,
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("as_of", Text, nullable=False),
    Column("components", Text, nullable=False),
    Column("total", REAL),
    Column("coverage", REAL, nullable=False),
    Column("input_hash", LargeBinary, nullable=False),
    Column("created_at", Text, nullable=False),
    PrimaryKeyConstraint("symbol", "as_of"),
    CheckConstraint("total IS NULL OR (total >= -100 AND total <= 100)", name="total"),
    CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage"),
    _date_ck("as_of"),
    _json_ck("components"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# QTUM theme decomposition (spec §6.1): rolling OLS of QTUM on SOXX, QQQ and the equal-weighted
# pure-play basket, ending at `as_of` (the last QTUM session).
theme_decomposition = Table(
    "theme_decomposition",
    metadata,
    Column("as_of", Text, primary_key=True),
    Column("n_sessions", Integer, nullable=False),
    Column("betas", Text, nullable=False),
    Column("attribution", Text, nullable=False),
    Column("r2", REAL),
    Column("quantum_partial_r2", REAL),
    Column("watchlist_weight_in_qtum", REAL),
    Column("basket_members", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    _date_ck("as_of"),
    _json_ck("betas"),
    _json_ck("attribution"),
    _json_ck("basket_members"),
    sqlite_strict=True,
)

REACTION_STATUSES = ("pending", "complete", "confounded", "no_data")

# Event-reaction check (spec §6.3), one row per event x affected ticker.
event_reactions = Table(
    "event_reactions",
    metadata,
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("t0", Text),  # NULL only when no anchor session exists (no_data)
    Column("benchmark", Text, nullable=False),
    Column("beta", REAL),
    Column("sigma_resid", REAL),
    Column("beta_fallback", Integer, nullable=False, server_default="0"),
    Column("car_1", REAL),
    Column("car_5", REAL),
    Column("car_20", REAL),
    Column("z_1", REAL),
    Column("z_5", REAL),
    Column("z_20", REAL),
    Column("ret_raw_1", REAL),  # raw stock return over [t0, t0+1], for implied vs realized
    Column("abn_volume", REAL),
    Column("reversal_ratio", REAL),
    Column("status", Text, nullable=False),
    Column("confounders", Text, nullable=False, server_default="[]"),
    Column("approx_time", Integer, nullable=False, server_default="0"),
    Column("note", Text),
    Column("computed_at", Text, nullable=False),
    PrimaryKeyConstraint("event_id", "symbol"),
    _in_ck("status", REACTION_STATUSES),
    _bool_ck("beta_fallback"),
    _bool_ck("approx_time"),
    _date_ck("t0", nullable=True),
    CheckConstraint("t0 IS NOT NULL OR status = 'no_data'", name="t0_required"),
    _json_ck("confounders"),
    Index(None, "symbol", "t0"),
    Index(None, "status"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# Weekly calibration report (spec §6.3).
calibration_reports = Table(
    "calibration_reports",
    metadata,
    Column("as_of", Text, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    _date_ck("as_of"),
    _json_ck("payload"),
    sqlite_strict=True,
)

# --------------------------------------------------------------------------- M10: conclusions

STANCE_VALUES = ("ACCUMULATE", "HOLD", "TRIM", "AVOID")
TILT_VALUES = ("PURE_PLAYS", "NEUTRAL", "QTUM")
HORIZON_VALUES = ("1m", "3m", "6m", "12m", "24m", "36m")


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


# One row per synthesis run that passed validation (spec §6.2). `stance` is the stance after
# hysteresis; a blocked flip keeps the previous stance with `held = 1` and the model's proposal in
# `proposed_stance`. Theme rows (kind 'theme', no symbol) hold a tilt between QTUM and the
# pure-plays. `evidence` is the id -> label/url map the run could cite (for rendering citations).
conclusions = Table(
    "conclusions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("symbol", Text, ForeignKey("tickers.symbol")),
    Column("as_of", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("stance", Text, nullable=False),
    Column("proposed_stance", Text, nullable=False),
    Column("held", Integer, nullable=False, server_default="0"),
    Column("hold_reason", Text),
    Column("confidence", REAL, nullable=False),
    Column("horizon", Text, nullable=False),
    Column("payload", Text, nullable=False),
    Column("evidence", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("input_hash", LargeBinary, nullable=False),
    Column("cost_micros", Micros, nullable=False, server_default="0"),
    Column("prev_id", Integer, ForeignKey("conclusions.id")),
    _in_ck("kind", ("ticker", "theme")),
    CheckConstraint("(kind = 'ticker') = (symbol IS NOT NULL)", name="symbol_kind"),
    CheckConstraint(
        f"(kind = 'ticker' AND stance IN ({_quoted(STANCE_VALUES)}) "
        f"AND proposed_stance IN ({_quoted(STANCE_VALUES)})) OR "
        f"(kind = 'theme' AND stance IN ({_quoted(TILT_VALUES)}) "
        f"AND proposed_stance IN ({_quoted(TILT_VALUES)}))",
        name="stance",
    ),
    CheckConstraint("held = 1 OR stance = proposed_stance", name="held_stance"),
    _bool_ck("held"),
    CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
    _in_ck("horizon", ("12m", "36m")),
    _date_ck("as_of"),
    _json_ck("payload"),
    _json_ck("evidence"),
    CheckConstraint("length(input_hash) = 32", name="input_hash_len"),
    CheckConstraint("cost_micros >= 0", name="cost"),
    Index(None, "symbol", "as_of"),
    Index(None, "kind", "id"),
    sqlite_strict=True,
)

# Forward excess return of each conclusion (spec §6.4), filled in as each horizon matures, with
# the two naive baselines judged on the same window ("always HOLD" and 90-day momentum).
conclusion_outcomes = Table(
    "conclusion_outcomes",
    metadata,
    Column(
        "conclusion_id",
        Integer,
        ForeignKey("conclusions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("horizon", Text, nullable=False),
    Column("benchmark", Text, nullable=False),
    Column("start_d", Text),
    Column("end_d", Text, nullable=False),
    Column("excess_return", REAL),
    Column("hit", Integer),
    Column("hold_hit", Integer),
    Column("momentum_stance", Text),
    Column("momentum_hit", Integer),
    Column("status", Text, nullable=False),
    Column("computed_at", Text, nullable=False),
    PrimaryKeyConstraint("conclusion_id", "horizon"),
    _in_ck("horizon", HORIZON_VALUES),
    _in_ck("status", ("pending", "complete")),
    CheckConstraint("hit IS NULL OR hit IN (0, 1)", name="hit_bool"),
    CheckConstraint("hold_hit IS NULL OR hold_hit IN (0, 1)", name="hold_hit_bool"),
    CheckConstraint("momentum_hit IS NULL OR momentum_hit IN (0, 1)", name="momentum_hit_bool"),
    CheckConstraint(
        "status = 'pending' OR (excess_return IS NOT NULL AND hit IS NOT NULL)",
        name="complete_has_values",
    ),
    _date_ck("start_d", nullable=True),
    _date_ck("end_d"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# Layer 3 of the research overlay (spec §6.6.1): each publish's base and published (overlay-
# adjusted) targets held as two buy-and-hold paper portfolios.
overlay_outcomes = Table(
    "overlay_outcomes",
    metadata,
    Column("profile", Text, nullable=False),
    Column("as_of", Text, nullable=False),
    Column("horizon", Text, nullable=False),
    Column("start_d", Text),
    Column("end_d", Text, nullable=False),
    Column("base_return", REAL),
    Column("adjusted_return", REAL),
    Column("status", Text, nullable=False),
    Column("computed_at", Text, nullable=False),
    PrimaryKeyConstraint("profile", "as_of", "horizon"),
    ForeignKeyConstraint(
        ["profile", "as_of"],
        ["profile_targets.profile", "profile_targets.as_of"],
        ondelete="CASCADE",
    ),
    _in_ck("horizon", HORIZON_VALUES),
    _in_ck("status", ("pending", "complete")),
    CheckConstraint(
        "status = 'pending' OR (base_return IS NOT NULL AND adjusted_return IS NOT NULL)",
        name="complete_has_values",
    ),
    _date_ck("start_d", nullable=True),
    _date_ck("end_d"),
    sqlite_strict=True,
    sqlite_with_rowid=False,
)

# Weekly brief archive (spec §6.2): deterministic digest, one per ISO week.
briefs = Table(
    "briefs",
    metadata,
    Column("as_of", Text, primary_key=True),
    Column("week", Text, nullable=False, unique=True),
    Column("payload", Text, nullable=False),
    Column("telegram_text", Text),
    Column("status", Text, nullable=False),
    Column("error", Text),
    Column("created_at", Text, nullable=False),
    _in_ck("status", ("done", "failed")),
    _date_ck("as_of"),
    _json_ck("payload"),
    sqlite_strict=True,
)

# Synthesis runs that failed validation twice (spec §6.2: "log failures"). The error text only;
# the rejected model output is never stored.
conclusion_failures = Table(
    "conclusion_failures",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("symbol", Text, ForeignKey("tickers.symbol")),
    Column("as_of", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("error", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    _in_ck("kind", ("ticker", "theme")),
    CheckConstraint("attempts >= 0", name="attempts"),
    CheckConstraint("length(error) <= 1000", name="error_len"),
    _date_ck("as_of"),
    Index(None, "created_at"),
    sqlite_strict=True,
)


# --------------------------------------------------------------------------- M11: escalation
# One row per (event, ticker) that met an escalation trigger (spec §5.2.5). Refused rows are final
# and record which cap refused them; they never run. `detail` holds step outcomes (no model text).
ESCALATION_TRIGGERS = ("materiality", "t1_risk")
ESCALATION_STATUSES = ("running", "done", "failed", "refused")
ESCALATION_REFUSALS = ("daily_cap", "ticker_cooldown")

escalations = Table(
    "escalations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("event_id", Integer, ForeignKey("events.id"), nullable=False),
    Column("symbol", Text, ForeignKey("tickers.symbol"), nullable=False),
    Column("trigger", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("refusal", Text),
    Column("research_run_id", Integer, ForeignKey("research_runs.id")),
    Column("conclusion_id", Integer, ForeignKey("conclusions.id")),
    Column("detail", Text, nullable=False, server_default="{}"),
    Column("created_at", Text, nullable=False),
    Column("finished_at", Text),
    _in_ck("trigger", ESCALATION_TRIGGERS),
    _in_ck("status", ESCALATION_STATUSES),
    CheckConstraint(
        "refusal IS NULL OR refusal IN (" + ",".join(f"'{r}'" for r in ESCALATION_REFUSALS) + ")",
        name="refusal",
    ),
    CheckConstraint("(status = 'refused') = (refusal IS NOT NULL)", name="refused_has_reason"),
    _json_ck("detail"),
    UniqueConstraint("event_id", "symbol"),
    Index(None, "created_at"),
    Index(None, "symbol", "created_at"),
    sqlite_strict=True,
)


# --------------------------------------------------------------------------- M12: universe review
# The monthly universe review (spec §6.7). One `universe_reviews` row per run: inserted as
# `running`, finished (`done`/`failed`) in one short write after all network and LLM work.
# `universe_candidates` are the gated proposals (code overrides the model; `proposed_action` keeps
# what the model said). `universe_evidence` holds the review's own evidence: T1 business excerpts
# from EDGAR and web-search results (untrusted text, excerpt ≤ 600 chars). Candidates aren't on the
# watchlist, so this evidence never enters `events` (Feed, classifier, scorecards).
UNIVERSE_REVIEW_KINDS = ("monthly", "manual")
UNIVERSE_REVIEW_STATUSES = ("running", "done", "failed")
UNIVERSE_TRACKS = ("pure_play",)  # M14 adds 'adjacent'
UNIVERSE_ACTIONS = ("add", "remove", "watch", "keep", "skip")
UNIVERSE_EVIDENCE_KINDS = ("business_excerpt", "web")

universe_reviews = Table(
    "universe_reviews",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("as_of", Text, nullable=False),
    Column("month", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("payload", Text, nullable=False, server_default="{}"),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text),
    Column("cost_micros", Micros, nullable=False, server_default="0"),
    Column("telegram_text", Text),
    Column("error", Text),
    Column("created_at", Text, nullable=False),
    Column("finished_at", Text),
    _in_ck("kind", UNIVERSE_REVIEW_KINDS),
    _in_ck("status", UNIVERSE_REVIEW_STATUSES),
    _date_ck("as_of"),
    CheckConstraint("month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'", name="month"),
    CheckConstraint("cost_micros >= 0", name="cost"),
    CheckConstraint("telegram_text IS NULL OR length(telegram_text) <= 4096", name="telegram_len"),
    CheckConstraint("error IS NULL OR length(error) <= 1000", name="error_len"),
    _json_ck("payload"),
    Index(None, "month", "status"),
    sqlite_strict=True,
)

universe_candidates = Table(
    "universe_candidates",
    metadata,
    Column(
        "review_id", Integer, ForeignKey("universe_reviews.id", ondelete="CASCADE"), nullable=False
    ),
    Column("symbol", Text, nullable=False),
    Column("track", Text, nullable=False, server_default="pure_play"),
    Column("action", Text, nullable=False),
    Column("proposed_action", Text),
    Column("cik", Text),
    Column("name", Text),
    Column("overlap", Text, nullable=False, server_default="{}"),
    Column("criteria", Text, nullable=False, server_default="{}"),
    Column("description", Text),
    Column("reasons", Text, nullable=False, server_default="[]"),
    Column("evidence_ids", Text, nullable=False, server_default="[]"),
    Column("gate_note", Text),
    PrimaryKeyConstraint("review_id", "symbol"),
    _in_ck("track", UNIVERSE_TRACKS),
    _in_ck("action", UNIVERSE_ACTIONS),
    CheckConstraint(
        "proposed_action IS NULL OR proposed_action IN ("
        + ",".join(f"'{a}'" for a in UNIVERSE_ACTIONS)
        + ")",
        name="proposed_action",
    ),
    CheckConstraint("description IS NULL OR length(description) <= 300", name="description_len"),
    _json_ck("overlap"),
    _json_ck("criteria"),
    _json_ck("reasons"),
    _json_ck("evidence_ids"),
    sqlite_strict=True,
)

universe_evidence = Table(
    "universe_evidence",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "review_id", Integer, ForeignKey("universe_reviews.id", ondelete="CASCADE"), nullable=False
    ),
    Column("symbol", Text),  # NULL: the IPO/SPAC sweep (no ticker yet)
    Column("kind", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("domain", Text, nullable=False),
    Column("trust_tier", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("excerpt", Text),
    Column("published_at", Text),
    Column("date_source", Text),
    Column("form", Text),
    Column("accession", Text),
    _in_ck("kind", UNIVERSE_EVIDENCE_KINDS),
    _in_ck("trust_tier", TRUST_TIERS),
    _excerpt_ck(),
    CheckConstraint("length(title) BETWEEN 1 AND 500", name="title_len"),
    CheckConstraint("url LIKE 'https://%' OR url LIKE 'http://%'", name="url_scheme"),
    Index(None, "review_id", "symbol"),
    sqlite_strict=True,
)
