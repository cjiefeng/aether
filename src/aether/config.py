"""Settings from env (pydantic-settings) and typed loaders for config/*.yaml.

Config files hold identifiers only: every YAML model forbids unknown keys, so free-text
commentary cannot sneak into config (and from there into prompts).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
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

    telegram_bot_token: SecretStr | None = Field(
        default=None, validation_alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_allowed_user_id: str | None = Field(
        default=None, validation_alias="TELEGRAM_ALLOWED_USER_ID"
    )
    telegram_chat_id: str | None = Field(default=None, validation_alias="TELEGRAM_CHAT_ID")

    @field_validator(
        "csrf_secret",
        "anthropic_api_key",
        "classifier_model",
        "synth_model",
        "sec_user_agent",
        "telegram_bot_token",
        "telegram_allowed_user_id",
        "telegram_chat_id",
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


def _load_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_watchlist(config_dir: Path) -> Watchlist:
    return Watchlist.model_validate(_load_yaml(config_dir / "watchlist.yaml"))


def load_sources(config_dir: Path) -> Sources:
    return Sources.model_validate(_load_yaml(config_dir / "sources.yaml"))


def get_settings() -> Settings:
    return Settings()
