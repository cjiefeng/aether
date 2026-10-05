"""Settings from env (pydantic-settings) and typed loaders for config/*.yaml.

Config files hold identifiers only: every YAML model forbids unknown keys, so free-text
commentary cannot sneak into config (and from there into prompts).
"""

from __future__ import annotations

from decimal import Decimal
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
    bind: str = Field(default="0.0.0.0:8000", validation_alias="AETHER_BIND")
    csrf_secret: SecretStr | None = Field(default=None, validation_alias="AETHER_CSRF_SECRET")
    log_level: str = Field(default="INFO", validation_alias="AETHER_LOG_LEVEL")
    command_rate_limit_per_hour: int = Field(
        default=10, validation_alias="AETHER_COMMAND_RATE_LIMIT_PER_HOUR"
    )
    backup_dir: Path | None = Field(default=None, validation_alias="AETHER_BACKUP_DIR")
    backup_keep_days: int = Field(default=14, validation_alias="AETHER_BACKUP_KEEP_DAYS")

    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    classifier_model: str | None = Field(default=None, validation_alias="CLASSIFIER_MODEL")
    synth_model: str | None = Field(default=None, validation_alias="SYNTH_MODEL")
    daily_llm_budget_usd: Decimal = Field(
        default=Decimal("3.00"), validation_alias="DAILY_LLM_BUDGET_USD"
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
        "classifier_model",
        "synth_model",
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


class TickerConfig(_Strict):
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")
    type: TickerType
    cik: str | None = Field(default=None, pattern=r"^\d{10}$")
    active: bool = True


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


class Sources(_Strict):
    domains: tuple[SourceDomain, ...]

    def tier_for(self, domain: str) -> TrustTier:
        """Exact or parent-domain match; anything not allow-listed is T3."""
        d = domain.lower().rstrip(".")
        for s in self.domains:
            if d == s.domain or d.endswith("." + s.domain):
                return s.tier
        return "T3"


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


class Rubric(_Strict):
    edgar_form_rules: tuple[FormRule, ...]
    edgar_8k_item_rules: tuple[ItemRule, ...]
    insider_selling: InsiderSellingRule
    going_concern: TextRule
    risk_flags: RiskFlagParams

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


class OverlayParams(_Strict):
    """Research overlay layer 1: filing hard rules (spec §6.6.1, M5)."""

    enabled: bool
    # An 8-K Item 3.01 (listing-compliance notice) zeroes the name for this many days.
    compliance_notice_days: int = Field(ge=1, le=3650)
    # A Form 25/15 for the common stock counts only once the stock has stopped trading: no
    # close for this many QTUM sessions.
    delisted_stale_sessions: int = Field(ge=1, le=250)
    # Filings the owner has reviewed and cleared (e.g. a Form 25 for an exchange transfer, or a
    # resolved compliance notice). Identifiers only.
    cleared_accessions: tuple[Annotated[str, Field(pattern=ACCESSION_RE)], ...] = ()


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


def get_settings() -> Settings:
    return Settings()
