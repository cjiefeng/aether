"""Settings from env (pydantic-settings) and typed loaders for config/*.yaml.

Config files hold identifiers only: every YAML model forbids unknown keys, so free-text
commentary cannot sneak into config (and from there into prompts).
"""

from __future__ import annotations

import re
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process settings. Secrets come from the environment only (S4); no env_file is read here."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    db_path: Path = Field(default=Path("data/aether.db"), validation_alias="AETHER_DB_PATH")
    config_dir: Path = Field(default=Path("config"), validation_alias="AETHER_CONFIG_DIR")
    # M7: `make eval` results (committed); the worker loads them into eval_runs at startup.
    eval_results_dir: Path = Field(
        default=Path("evals/results"), validation_alias="AETHER_EVAL_RESULTS_DIR"
    )
    bind: str = Field(default="0.0.0.0:8000", validation_alias="AETHER_BIND")
    csrf_secret: SecretStr | None = Field(default=None, validation_alias="AETHER_CSRF_SECRET")
    log_level: str = Field(default="INFO", validation_alias="AETHER_LOG_LEVEL")
    command_rate_limit_per_hour: int = Field(
        default=10, validation_alias="AETHER_COMMAND_RATE_LIMIT_PER_HOUR"
    )
    backup_dir: Path | None = Field(default=None, validation_alias="AETHER_BACKUP_DIR")
    backup_keep_days: int = Field(default=14, validation_alias="AETHER_BACKUP_KEEP_DAYS")

    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    # M7: owner decision 2026-10-05: Sonnet 5.5.
    classifier_model: str = Field(default="claude-sonnet-5-5", validation_alias="CLASSIFIER_MODEL")
    # M10: conclusions (no tools). Owner decision 2026-10-06: Opus 5.5.
    synth_model: str = Field(default="claude-opus-5-5", validation_alias="SYNTH_MODEL")
    # M6: web-search research runs (the only tool-enabled calls). Owner decision 2026-10-05: Opus.
    research_model: str = Field(default="claude-opus-5-5", validation_alias="RESEARCH_MODEL")
    # M6: the one-time 12-month backfill (Message Batches) runs automatically once an API key is
    # set; this switch exists so an isolated test stack can turn it off.
    research_backfill: bool = Field(default=True, validation_alias="RESEARCH_BACKFILL")
    daily_llm_budget_usd: Decimal = Field(
        default=Decimal("5.00"), validation_alias="DAILY_LLM_BUDGET_USD"
    )
    sec_user_agent: str | None = Field(default=None, validation_alias="SEC_USER_AGENT")
    # Massive (formerly Polygon) free "Stocks Basic" key: price fallback when yfinance fails.
    massive_api_key: SecretStr | None = Field(default=None, validation_alias="MASSIVE_API_KEY")

    telegram_bot_token: SecretStr | None = Field(
        default=None, validation_alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_allowed_user_id: str | None = Field(
        default=None, validation_alias="TELEGRAM_ALLOWED_USER_ID"
    )
    telegram_chat_id: str | None = Field(default=None, validation_alias="TELEGRAM_CHAT_ID")

    # S2 (M5): dashboard password (scrypt hash, see security/auth.py) and session-cookie key.
    # The app refuses to start without both; the worker doesn't use them.
    dashboard_password_hash: SecretStr | None = Field(
        default=None, validation_alias="AETHER_DASHBOARD_PASSWORD_HASH"
    )
    session_secret: SecretStr | None = Field(default=None, validation_alias="AETHER_SESSION_SECRET")

    # S8 (M5): optional read-only Tiger Brokers holdings sync (worker only).
    tiger_id: str | None = Field(default=None, validation_alias="TIGER_ID")
    tiger_private_key: SecretStr | None = Field(default=None, validation_alias="TIGER_PRIVATE_KEY")
    tiger_account: SecretStr | None = Field(default=None, validation_alias="TIGER_ACCOUNT")

    @field_validator(
        "csrf_secret",
        "anthropic_api_key",
        "sec_user_agent",
        "massive_api_key",
        "telegram_bot_token",
        "telegram_allowed_user_id",
        "telegram_chat_id",
        "dashboard_password_hash",
        "session_secret",
        "tiger_id",
        "tiger_private_key",
        "tiger_account",
        "backup_dir",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        # docker compose passes unset vars as "" — treat them as absent.
        return None if v == "" else v

    @field_validator(
        "research_model", "classifier_model", "synth_model", "research_backfill", mode="before"
    )
    @classmethod
    def _empty_is_default(cls, v: object, info: ValidationInfo) -> object:
        # docker compose passes unset vars as "": fall back to the field default.
        if v == "":
            assert info.field_name is not None
            return cls.model_fields[info.field_name].default
        return v

    @field_validator("research_model", "classifier_model", "synth_model", mode="after")
    @classmethod
    def _model_id(cls, v: str, info: ValidationInfo) -> str:
        if not re.fullmatch(r"claude-[a-z0-9\-]{1,60}", v):
            raise ValueError(f"{info.field_name} must be a Claude model id")
        return v

    @property
    def resolved_backup_dir(self) -> Path:
        return self.backup_dir or self.db_path.parent / "backups"

    @property
    def bind_host_port(self) -> tuple[str, int]:
        host, _, port = self.bind.rpartition(":")
        return host or "0.0.0.0", int(port)  # noqa: S104


# --------------------------------------------------------------------------- YAML config


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


TickerType = Literal["etf", "pure_play", "benchmark", "context"]


Alias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 .&\-]{1,40}$")]


class TickerConfig(_Strict):
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")
    type: TickerType
    cik: str | None = Field(default=None, pattern=r"^\d{10}$")
    active: bool = True
    # M6: company names used only to match news items to tickers (identifiers, not commentary).
    aliases: tuple[Alias, ...] = ()


class Watchlist(_Strict):
    tickers: tuple[TickerConfig, ...]

    @field_validator("tickers")
    @classmethod
    def _unique(cls, v: tuple[TickerConfig, ...]) -> tuple[TickerConfig, ...]:
        symbols = [t.symbol for t in v]
        dupes = {s for s in symbols if symbols.count(s) > 1}
        if dupes:
            raise ValueError(f"duplicate symbols in watchlist: {sorted(dupes)}")
        return v

    def by_type(self, t: TickerType) -> tuple[TickerConfig, ...]:
        return tuple(x for x in self.tickers if x.type == t)


TrustTier = Literal["T1", "T2", "T3"]


class SourceDomain(_Strict):
    domain: str = Field(pattern=r"^[a-z0-9.\-]+\.[a-z]{2,}$")
    tier: Literal["T1", "T2"]


DOMAIN_RE = r"^[a-z0-9.\-]+\.[a-z]{2,}$"


class Feed(_Strict):
    """One RSS/Atom feed (M6). `symbol` pins a company's own IR feed to its ticker."""

    id: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    url: str = Field(pattern=r"^https://[a-z0-9.\-]+\.[a-z]{2,}/\S*$")
    symbol: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")


Keyword = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 .\-]{1,40}$")]


class Sources(_Strict):
    domains: tuple[SourceDomain, ...]
    # M6: RSS feeds, wire/mirror domains whose copies never count as independent sources, and the
    # theme keywords that keep an industry item with no watchlist company in it.
    feeds: tuple[Feed, ...] = ()
    syndicators: tuple[Annotated[str, Field(pattern=DOMAIN_RE)], ...] = ()
    theme_keywords: tuple[Keyword, ...] = ()

    @field_validator("feeds")
    @classmethod
    def _feed_ids_unique(cls, v: tuple[Feed, ...]) -> tuple[Feed, ...]:
        ids = [f.id for f in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate feed ids")
        return v

    def tier_for(self, domain: str) -> TrustTier:
        """Exact or parent-domain match; anything not allow-listed is T3."""
        d = domain.lower().rstrip(".")
        for s in self.domains:
            if d == s.domain or d.endswith("." + s.domain):
                return s.tier
        return "T3"

    def is_syndicator(self, domain: str) -> bool:
        d = domain.lower().rstrip(".")
        return any(d == s or d.endswith("." + s) for s in self.syndicators)

    def allowed_domains(self) -> tuple[str, ...]:
        """T1 + T2 domains: the research runs' web-search allow-list (spec §4)."""
        return tuple(s.domain for s in self.domains)


RiskCategory = Literal[
    "dilution",
    "insider_selling",
    "exec_departure",
    "going_concern",
    "delisting_or_compliance",
]
Materiality = Annotated[int, Field(ge=1, le=5)]
RULE_ID = r"^[a-z0-9_]{3,64}$"


class FormRule(_Strict):
    rule_id: str = Field(pattern=RULE_ID)
    forms: tuple[str, ...] = Field(min_length=1)
    category: RiskCategory
    materiality: Materiality


class ItemRule(_Strict):
    rule_id: str = Field(pattern=RULE_ID)
    item: str = Field(pattern=r"^\d\.\d{2}$")
    category: Literal["earnings_release", "exec_departure", "delisting_or_compliance", "dilution"]
    materiality: Materiality


class InsiderSellingRule(_Strict):
    rule_id: str = Field(pattern=RULE_ID)
    materiality: Materiality
    materiality_10b5_1_only: Materiality


class TextRule(_Strict):
    rule_id: str = Field(pattern=RULE_ID)
    materiality: Materiality


class RiskFlagParams(_Strict):
    lockup_window_days: int = Field(gt=0)
    insider_cluster_min_insiders: int = Field(ge=2)
    insider_cluster_window_days: int = Field(gt=0)
    shelf_active_days: int = Field(gt=0)
    atm_active_days: int = Field(gt=0)


EventClass = Literal["SIGNAL", "NOISE", "RISK"]
# The spec §5.1 category → class mapping. The rubric YAML must match it exactly (and the DB CHECK
# on event_classifications.category lists the same names).
CATEGORY_CLASS: dict[str, EventClass] = {
    "qbi_stage_change": "SIGNAL",
    "roadmap_hit": "SIGNAL",
    "roadmap_slip": "SIGNAL",
    "logical_qubit_milestone": "SIGNAL",
    "verified_advantage": "SIGNAL",
    "revenue_quality": "SIGNAL",
    "contract_with_value": "SIGNAL",
    "m_and_a": "SIGNAL",
    "earnings_release": "SIGNAL",
    "physical_qubit_count": "NOISE",
    "partnership_no_value": "NOISE",
    "analyst_rating": "NOISE",
    "synthetic_benchmark": "NOISE",
    "listicle_or_momentum": "NOISE",
    "dilution": "RISK",
    "insider_selling": "RISK",
    "lockup_expiry": "RISK",
    "short_interest_spike": "RISK",
    "resource_estimate_shift": "RISK",
    "pqc_deadline_change": "RISK",
    "exec_departure": "RISK",
    "going_concern": "RISK",
    "short_report": "RISK",
    "guidance_cut": "RISK",
    "delisting_or_compliance": "RISK",
}
Definition = Annotated[str, Field(min_length=10, max_length=400)]


class CategoryRubric(_Strict):
    cls: EventClass = Field(alias="class")
    materiality: tuple[Materiality, Materiality]  # typical range, shown to the model as guidance
    definition: Definition

    @field_validator("materiality")
    @classmethod
    def _range(cls, v: tuple[int, int]) -> tuple[int, int]:
        if v[0] > v[1]:
            raise ValueError("materiality range must be [low, high]")
        return v


def _compile_all(patterns: tuple[str, ...]) -> tuple[str, ...]:
    for p in patterns:
        try:
            re.compile(p)
        except re.error as exc:
            raise ValueError(f"bad regex {p!r}: {exc}") from None
    return patterns


class HeadlineRule(_Strict):
    rule_id: str = Field(pattern=RULE_ID)
    category: Literal["analyst_rating", "listicle_or_momentum"]
    materiality: Materiality
    confidence: Annotated[float, Field(gt=0, le=1)]
    patterns: tuple[str, ...] = Field(min_length=1)

    @field_validator("patterns")
    @classmethod
    def _compiles(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _compile_all(v)


class ClassifierRubric(_Strict):
    """The M7 LLM rubric and deterministic news rules (spec §5.1, §5.2)."""

    materiality_anchors: dict[Materiality, Definition]
    categories: dict[str, CategoryRubric]
    headline_rules: tuple[HeadlineRule, ...] = ()
    noise_domains: tuple[Annotated[str, Field(pattern=DOMAIN_RE)], ...] = ()
    injection_patterns: tuple[str, ...] = Field(min_length=1)

    @field_validator("injection_patterns")
    @classmethod
    def _compiles(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _compile_all(v)

    @field_validator("materiality_anchors")
    @classmethod
    def _anchors(cls, v: dict[int, str]) -> dict[int, str]:
        if set(v) != {1, 2, 3, 4, 5}:
            raise ValueError("materiality_anchors must define exactly 1-5")
        return dict(sorted(v.items()))

    @field_validator("categories")
    @classmethod
    def _categories(cls, v: dict[str, CategoryRubric]) -> dict[str, CategoryRubric]:
        if set(v) != set(CATEGORY_CLASS):
            missing = sorted(set(CATEGORY_CLASS) - set(v))
            extra = sorted(set(v) - set(CATEGORY_CLASS))
            raise ValueError(f"categories must be the spec set (missing {missing}, extra {extra})")
        wrong = [k for k, c in v.items() if c.cls != CATEGORY_CLASS[k]]
        if wrong:
            raise ValueError(f"category/class mismatch for {wrong}")
        return v


class ShortInterestRule(_Strict):
    """FINRA short-interest ingest and the `short_interest_spike` rule (spec §5.1, §5.2; M8).

    Short % is short shares ÷ shares outstanding (FINRA publishes no float figure)."""

    rule_id: str = Field(pattern=RULE_ID)
    rise_pp: Annotated[float, Field(gt=0, le=100)]  # fire on a rise of at least this many points
    level_pct: Annotated[float, Field(gt=0, le=100)]  # ... or on crossing above this level
    materiality: Materiality
    months_back: int = Field(ge=1, le=60)  # backfill / probe window for FINRA files
    min_age_days: int = Field(ge=0, le=30)  # files appear about a week after settlement


class Rubric(_Strict):
    edgar_form_rules: tuple[FormRule, ...]
    edgar_8k_item_rules: tuple[ItemRule, ...]
    insider_selling: InsiderSellingRule
    going_concern: TextRule
    risk_flags: RiskFlagParams
    short_interest: ShortInterestRule
    classifier: ClassifierRubric

    @field_validator("edgar_form_rules")
    @classmethod
    def _forms_unique(cls, v: tuple[FormRule, ...]) -> tuple[FormRule, ...]:
        forms = [f for r in v for f in r.forms]
        if len(forms) != len(set(forms)):
            raise ValueError("a form appears in more than one edgar_form_rule")
        return v


class AlertsConfig(_Strict):
    """`config/alerts.yaml`: what reaches Telegram (M3). Numbers only."""

    risk_event_min_materiality: Materiality
    # Only events published this recently alert, so a backfill never floods the chat.
    event_lookback_days: int = Field(gt=0, le=30)
    reminder_days: tuple[int, ...] = Field(min_length=1)  # lock-up / earnings T-N reminders
    job_failing_hours: int = Field(gt=0)
    pending_expiry_hours: int = Field(gt=0)
    max_attempts: int = Field(ge=1)
    max_sends_per_run: int = Field(ge=1)

    @field_validator("reminder_days")
    @classmethod
    def _reminders(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if any(d < 0 for d in v) or len(set(v)) != len(v):
            raise ValueError("reminder_days must be distinct, non-negative day counts")
        return tuple(sorted(v, reverse=True))


Profile = Literal["safe", "medium", "aggressive"]
PROFILES: tuple[Profile, ...] = ("safe", "medium", "aggressive")
Fraction = Annotated[float, Field(ge=0, le=1)]


class BacktestParams(_Strict):
    estimation_window: int = Field(ge=20)  # sessions; also the in-sample warm-up
    min_sessions: int = Field(ge=2)  # a name joins once it has this many return sessions
    cost_bps: float = Field(ge=0, le=100)  # per unit of turnover (sum of |weight change|)
    rebalance: Literal["monthly"]
    momentum_lookback: int = Field(ge=2)
    momentum_top_n: int = Field(ge=1)
    min_var_iterations: int = Field(ge=10, le=100_000)
    annualization: int = Field(ge=1)

    @field_validator("min_sessions")
    @classmethod
    def _min_sessions(cls, v: int, info: ValidationInfo) -> int:
        window = info.data.get("estimation_window")
        if window is not None and v > window:
            raise ValueError("min_sessions must be <= estimation_window")
        return v


class ProfileParams(_Strict):
    # Fixed by the owner (spec §1.4, §6.5 amended 2026-10-04): risk appetite is the size of the
    # QTUM core. The backtest chooses only the sleeve method, never this weight.
    qtum_weight: Fraction
    max_per_name: Annotated[float, Field(gt=0, le=1)]
    vol_limit_x: Annotated[float, Field(gt=0)] | None  # x QTUM's OOS volatility; None = shown only
    max_dd_limit_pp: Annotated[float, Field(ge=0)] | None  # QTUM's OOS max DD + N pp; None = shown
    rank_metric: Literal["cvar95_low", "sortino_high"]


class RebalanceParams(_Strict):
    """The rebalance no-trade band (spec §6.6, M5)."""

    drift_abs: Annotated[float, Field(ge=0, le=1)]  # trade if |drift| >= this ...
    drift_rel: Annotated[float, Field(ge=0)]  # ... or >= this fraction of the target weight
    min_trade_usd: Annotated[Decimal, Field(ge=0)]


class PublishParams(_Strict):
    """Monthly target publishing (spec §6.6, M5)."""

    # A non-quarantined event at or above this materiality on a pure-play suggests an
    # off-cycle review (alert only; targets never change automatically).
    off_cycle_min_materiality: Materiality


ACCESSION_RE = r"^\d{10}-\d{2}-\d{6}$"
# M10 conclusions: ticker stances (spec §6.2) and theme tilts.
STANCES = ("ACCUMULATE", "HOLD", "TRIM", "AVOID")
Stance = Literal["ACCUMULATE", "HOLD", "TRIM", "AVOID"]
TILTS = ("PURE_PLAYS", "NEUTRAL", "QTUM")
HORIZONS = ("1m", "3m", "6m", "12m", "24m", "36m")


class OverlayParams(_Strict):
    """Research overlay layer 1: filing hard rules (spec §6.6.1, M5) and the dilution / runway
    haircuts (M9)."""

    enabled: bool
    # An 8-K Item 3.01 (listing-compliance notice) zeroes the name for this many days.
    compliance_notice_days: int = Field(ge=1, le=3650)
    # A Form 25/15 for the common stock counts only once the stock has stopped trading: no
    # close for this many QTUM sessions.
    delisted_stale_sessions: int = Field(ge=1, le=250)
    # Filings the owner has reviewed and cleared (e.g. a Form 25 for an exchange transfer, or a
    # resolved compliance notice). Identifiers only.
    cleared_accessions: tuple[Annotated[str, Field(pattern=ACCESSION_RE)], ...] = ()
    # M9 haircuts (permanent-loss risk from fundamentals): fully diluted shares up more than
    # `dilution_yoy_max` year on year, and cash runway under `runway_min_months`.
    dilution_yoy_max: float = Field(gt=0, le=10)
    dilution_multiplier: float = Field(ge=0, le=1)
    runway_min_months: float = Field(gt=0, le=120)
    runway_multiplier: float = Field(ge=0, le=1)
    # M10 layer 2: stance multipliers (after hysteresis). While a ticker's track record is
    # unproven, its multiplier is clamped to `unproven_clamp`. Layer 1 is never clamped.
    stance_multipliers: dict[Stance, Annotated[float, Field(ge=0, le=2)]]
    unproven_clamp: tuple[Annotated[float, Field(ge=0, le=2)], Annotated[float, Field(ge=0, le=2)]]

    @field_validator("stance_multipliers")
    @classmethod
    def _all_stances(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != set(STANCES):
            raise ValueError(f"stance_multipliers must name exactly {list(STANCES)}")
        return {k: v[k] for k in STANCES}

    @field_validator("unproven_clamp")
    @classmethod
    def _clamp_order(cls, v: tuple[float, float]) -> tuple[float, float]:
        if v[0] > v[1]:
            raise ValueError("unproven_clamp must be [low, high]")
        return v


class StrategiesConfig(_Strict):
    """`config/strategies.yaml`: backtest parameters and risk profiles (M4); rebalance band,
    monthly publishing and the research overlay (M5). Numbers and identifiers only."""

    backtest: BacktestParams
    profiles: dict[Profile, ProfileParams]
    rebalance: RebalanceParams
    publish: PublishParams
    overlay: OverlayParams

    @field_validator("profiles")
    @classmethod
    def _all_profiles(cls, v: dict[Profile, ProfileParams]) -> dict[Profile, ProfileParams]:
        if set(v) != set(PROFILES):
            raise ValueError(f"profiles must be exactly {list(PROFILES)}")
        return {p: v[p] for p in PROFILES}


def _load_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_watchlist(config_dir: Path) -> Watchlist:
    return Watchlist.model_validate(_load_yaml(config_dir / "watchlist.yaml"))


def load_sources(config_dir: Path) -> Sources:
    return Sources.model_validate(_load_yaml(config_dir / "sources.yaml"))


def load_rubric(config_dir: Path) -> Rubric:
    return Rubric.model_validate(_load_yaml(config_dir / "rubric.yaml"))


def load_alerts_config(config_dir: Path) -> AlertsConfig:
    return AlertsConfig.model_validate(_load_yaml(config_dir / "alerts.yaml"))


def load_strategies(config_dir: Path) -> StrategiesConfig:
    return StrategiesConfig.model_validate(_load_yaml(config_dir / "strategies.yaml"))


class OptionsConfig(_Strict):
    """`config/options.yaml`: daily options snapshot quality gates (spec §6.8, M5). Research
    only: nothing here feeds sizing or trades."""

    max_days: int = Field(ge=30, le=730)  # expiries considered (calendar days ahead)
    max_expiries: int = Field(ge=1, le=24)
    min_open_interest: int = Field(ge=0)  # per contract used for ATM IV
    max_spread_pct: Annotated[float, Field(gt=0, le=2)]  # (ask - bid) / mid
    term_days: tuple[int, ...] = Field(min_length=1)  # ATM IV interpolated at these horizons


def load_options_config(config_dir: Path) -> OptionsConfig:
    return OptionsConfig.model_validate(_load_yaml(config_dir / "options.yaml"))


# --------------------------------------------------------------------------- scores (M9)

Anchor = tuple[float, float]


class AnchorMap(_Strict):
    """A piecewise-linear map from a raw metric to a score in [-1, 1], clamped at both ends.
    Points are (metric value, score) in strictly increasing metric order."""

    points: tuple[Anchor, ...] = Field(min_length=2)

    @field_validator("points")
    @classmethod
    def _ordered(cls, v: tuple[Anchor, ...]) -> tuple[Anchor, ...]:
        xs = [x for x, _ in v]
        if any(b <= a for a, b in pairwise(xs)):
            raise ValueError("anchor points must be in strictly increasing metric order")
        if any(not -1.0 <= y <= 1.0 for _, y in v):
            raise ValueError("anchor scores must be within [-1, 1]")
        return v

    def score(self, x: float) -> float:
        pts = self.points
        if x <= pts[0][0]:
            return pts[0][1]
        for (x0, y0), (x1, y1) in pairwise(pts):
            if x <= x1:
                return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return pts[-1][1]


SCORE_COMPONENTS = (
    "fundamentals",
    "dilution",
    "signal_momentum",
    "risk_load",
    "short_interest",
    "catalyst_position",
    "noise_ratio",
    "price_context",
    "market_reaction",
)


class ScorecardParams(_Strict):
    weights: dict[str, float]
    half_life_days: float = Field(gt=0, le=3650)
    event_lookback_days: int = Field(ge=1, le=3650)
    momentum_scale: float = Field(gt=0)
    risk_scale: float = Field(gt=0)
    open_flag_penalty: float = Field(ge=0)
    noise_window_days: int = Field(ge=1, le=365)
    noise_min_events: int = Field(ge=1)
    hype_noise_ratio: float = Field(ge=0, le=1)
    catalyst_horizon_days: int = Field(ge=1, le=3650)
    reaction_window_days: int = Field(ge=1, le=3650)
    atm_active_score: float = Field(ge=-1, le=1)
    shelf_active_score: float = Field(ge=-1, le=1)
    anchors: dict[str, AnchorMap]

    @field_validator("weights")
    @classmethod
    def _weights(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != set(SCORE_COMPONENTS):
            raise ValueError(f"weights must name exactly {list(SCORE_COMPONENTS)}")
        if any(w < 0 for w in v.values()) or sum(v.values()) <= 0:
            raise ValueError("weights must be >= 0 with a positive sum")
        return {k: v[k] for k in SCORE_COMPONENTS}

    @field_validator("anchors")
    @classmethod
    def _anchors(cls, v: dict[str, AnchorMap]) -> dict[str, AnchorMap]:
        if set(v) != set(ANCHOR_METRICS):
            raise ValueError(f"anchors must name exactly {list(ANCHOR_METRICS)}")
        return v


ANCHOR_METRICS = (
    "revenue_growth",
    "runway_months",
    "ev_sales",
    "fd_yoy",
    "instruments_pct",
    "short_pct_shares_out",
    "days_to_cover",
    "short_change_pp",
    "catalysts_upcoming",
    "catalyst_hit_rate",
    "noise_ratio",
    "drawdown_52w",
    "rel_perf_90d",
    "mean_z5",
)


class ReactionParams(_Strict):
    estimation_window: int = Field(ge=20, le=500)
    min_sessions: int = Field(ge=10, le=500)
    min_sigma_sessions: int = Field(ge=5, le=500)
    confound_min_materiality: int = Field(ge=1, le=5)
    reversal_min_abs_car1: float = Field(ge=0, le=1)
    volume_window: int = Field(ge=5, le=250)


class CalibrationParams(_Strict):
    min_n: int = Field(ge=1)
    noise_like_signal_mean_abs_z5: float = Field(gt=0)
    noise_like_signal_pct_z5_gt2: float = Field(ge=0, le=1)
    signal_ignored_mean_abs_z5: float = Field(gt=0)
    high_materiality: int = Field(ge=1, le=5)
    high_materiality_max_abs_z5: float = Field(gt=0)


class ConclusionParams(_Strict):
    """Stance hysteresis and synthesis context (spec §6.2, M10)."""

    # Scorecard-total band boundaries (ascending): AVOID < avoid_below <= TRIM < trim_below <=
    # HOLD < accumulate_from <= ACCUMULATE.
    avoid_below: float = Field(ge=-100, le=100)
    trim_below: float = Field(ge=-100, le=100)
    accumulate_from: float = Field(ge=-100, le=100)
    threshold_margin: float = Field(ge=0, le=200)
    consecutive_days: int = Field(ge=1, le=60)
    cooldown_days: int = Field(ge=0, le=365)
    trigger_min_materiality: Materiality
    context_days: int = Field(ge=7, le=365)
    max_events: int = Field(ge=5, le=300)
    keep_min_materiality: Materiality  # events at/above this are kept before filling by recency
    stance_max_age_days: int = Field(ge=1, le=365)  # older stances don't reach the overlay

    @field_validator("accumulate_from")
    @classmethod
    def _ordered(cls, v: float, info: ValidationInfo) -> float:
        a, t = info.data.get("avoid_below"), info.data.get("trim_below")
        if a is None or t is None or not a < t < v:
            raise ValueError("need avoid_below < trim_below < accumulate_from")
        return v


class TrackRecordParams(_Strict):
    """Conclusion track record (spec §6.4, M10)."""

    # HOLD is a hit when |excess return| is below this band, per horizon.
    hold_band: dict[str, float]  # keys: exactly HORIZONS (validated)
    min_mature_calls: int = Field(ge=1, le=1000)  # mature 6m calls before a stance is "proven"
    momentum_lookback_days: int = Field(ge=5, le=365)
    confidence_buckets: tuple[float, ...] = Field(min_length=2)  # ascending edges in [0, 1]

    @field_validator("hold_band")
    @classmethod
    def _all_horizons(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != set(HORIZONS) or any(not 0 < b < 10 for b in v.values()):
            raise ValueError(f"hold_band must give a positive band for each of {list(HORIZONS)}")
        return {h: v[h] for h in HORIZONS}

    @field_validator("confidence_buckets")
    @classmethod
    def _edges(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if v[0] != 0 or v[-1] != 1 or any(b <= a for a, b in pairwise(v)):
            raise ValueError("confidence_buckets must rise strictly from 0 to 1")
        return v


class WeightsConfig(_Strict):
    """`config/weights.yaml`: scorecard weights and anchors, reaction-engine and calibration
    parameters (spec §6.1, §6.3, M9); stance hysteresis and the track record (M10). Numbers only;
    initial values for owner review."""

    scorecard: ScorecardParams
    reactions: ReactionParams
    calibration: CalibrationParams
    conclusions: ConclusionParams
    track_record: TrackRecordParams


def load_weights(config_dir: Path) -> WeightsConfig:
    return WeightsConfig.model_validate(_load_yaml(config_dir / "weights.yaml"))


# --------------------------------------------------------------------------- catalysts (M8)

CatalystKind = Literal["roadmap", "program", "regulatory", "lockup"]
ResolveCategory = Literal["roadmap_hit", "roadmap_slip", "qbi_stage_change"]
SYMBOL_RE = r"^[A-Z][A-Z0-9.\-]{0,9}$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
# Titles name the milestone and its stated timing; nothing descriptive (that belongs in facts).
Title = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 .,:()\-]{2,119}$")]


class SeedCatalyst(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    symbol: str | None = Field(default=None, pattern=SYMBOL_RE)
    title: Title
    kind: CatalystKind
    window_start: str = Field(pattern=DATE_RE)
    window_end: str | None = Field(default=None, pattern=DATE_RE)  # None = no stated end
    fact_id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    source_url: str = Field(pattern=r"^https://\S+$")
    keywords: tuple[Keyword, ...] = ()  # product/program names that tie an event to it
    resolve_categories: tuple[ResolveCategory, ...] = ()

    @field_validator("window_end")
    @classmethod
    def _window(cls, v: str | None, info: ValidationInfo) -> str | None:
        start = info.data.get("window_start")
        if v is not None and start is not None and v < start:
            raise ValueError("window_end before window_start")
        return v

    @field_validator("resolve_categories")
    @classmethod
    def _needs_keywords(cls, v: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        if v and not info.data.get("keywords"):
            raise ValueError("event resolution needs at least one keyword")
        return v


class CatalystRules(_Strict):
    """Deterministic resolution parameters (catalysts/resolve.py). Initial values for review."""

    min_materiality: Materiality  # post-cap, non-quarantined events only
    lead_days: int = Field(ge=0, le=365)  # events this long before window_start still count
    grace_days: int = Field(ge=0, le=365)  # ... and after window_end; then `window_passed`
    earnings_match_days: int = Field(ge=0, le=14)  # 8-K 2.02 within ± this many days
    lockup_lookahead_days: int = Field(ge=1, le=730)


class CatalystsConfig(_Strict):
    """`config/catalysts_seed.yaml` (M8): identifiers, dates, fact ids and source URLs only."""

    rules: CatalystRules
    catalysts: tuple[SeedCatalyst, ...]

    @field_validator("catalysts")
    @classmethod
    def _unique(cls, v: tuple[SeedCatalyst, ...]) -> tuple[SeedCatalyst, ...]:
        ids = [c.id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate catalyst ids")
        return v


def load_catalysts_config(
    config_dir: Path, fact_ids: set[str] | None = None, symbols: set[str] | None = None
) -> CatalystsConfig:
    """Load and cross-check against facts.yaml and the watchlist (unknown ids are errors)."""
    cfg = CatalystsConfig.model_validate(_load_yaml(config_dir / "catalysts_seed.yaml"))
    if fact_ids is None:
        from aether.facts import load_facts

        fact_ids = {f.id for f in load_facts(config_dir)}
    if symbols is None:
        symbols = {t.symbol for t in load_watchlist(config_dir).tickers}
    for c in cfg.catalysts:
        if c.fact_id not in fact_ids:
            raise ValueError(f"catalyst {c.id}: unknown fact_id {c.fact_id!r}")
        if c.symbol is not None and c.symbol not in symbols:
            raise ValueError(f"catalyst {c.id}: symbol {c.symbol!r} is not on the watchlist")
    return cfg


Usd = Annotated[Decimal, Field(ge=0)]


class ModelPrice(_Strict):
    """USD per million tokens (Anthropic first-party list prices)."""

    input: Usd
    output: Usd
    cache_write: Usd  # 5-minute cache write
    cache_read: Usd


class ResearchParams(_Strict):
    max_tokens: int = Field(ge=256, le=64_000)
    effort: Literal["low", "medium", "high"]
    sweep_max_uses: int = Field(ge=1, le=20)
    sweep_days: int = Field(ge=1, le=14)
    backfill_max_uses: int = Field(ge=1, le=20)
    backfill_months: int = Field(ge=1, le=24)
    # Worst-case input-token allowance per search for the budget estimate (results are input).
    est_input_tokens_per_search: int = Field(ge=0)
    est_base_input_tokens: int = Field(ge=0)


class ClassifyParams(_Strict):
    """M7 classifier call parameters."""

    max_tokens: int = Field(ge=256, le=32_000)
    effort: Literal["low", "medium", "high"]
    batch_threshold: int = Field(ge=1)  # a backlog above this goes to one Message Batch
    batch_max_items: int = Field(ge=1, le=10_000)
    max_attempts: int = Field(ge=1, le=5)
    max_per_run: int = Field(ge=1, le=1000)


class SynthesisParams(_Strict):
    """M10 conclusion call parameters (no tools, ever)."""

    max_tokens: int = Field(ge=1024, le=32_000)
    effort: Literal["low", "medium", "high"]
    max_attempts: int = Field(ge=1, le=3)  # an invalid answer is retried once (spec §6.2)


class LlmConfig(_Strict):
    """`config/llm.yaml` (M6, M7, M10): prices, research, classifier and synthesis parameters.
    Numbers and identifiers only."""

    prices: dict[Annotated[str, Field(pattern=r"^claude-[a-z0-9\-]+$")], ModelPrice]
    web_search_per_1k: Usd
    batch_discount: Annotated[Decimal, Field(gt=0, le=1)]  # batch tokens cost this fraction
    budget_alert_fraction: Annotated[Decimal, Field(gt=0, lt=1)]
    chars_per_token: Annotated[int, Field(ge=1, le=10)]  # input estimate for the budget guard
    research: ResearchParams
    classify: ClassifyParams
    synthesis: SynthesisParams


def load_llm_config(config_dir: Path) -> LlmConfig:
    return LlmConfig.model_validate(_load_yaml(config_dir / "llm.yaml"))


def get_settings() -> Settings:
    return Settings()
