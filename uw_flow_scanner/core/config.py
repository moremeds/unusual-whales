from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SchedulerConfig(BaseModel):
    market_hours_only: bool = True
    poll_interval_seconds: int = 30


class ScoringConfig(BaseModel):
    score_threshold: int = 75
    tier1_concurrency: int = 10
    tier1_timeout_seconds: int = 5
    tier2_timeout_seconds: int = 30
    alert_cooldown_seconds: int = 600

    @field_validator("score_threshold")
    @classmethod
    def threshold_in_range(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError(f"score_threshold must be 0-100, got {v}")
        return v


class ModelRef(BaseModel):
    provider: str = "anthropic"
    model: str


class ModelsConfig(BaseModel):
    tier1: ModelRef
    tier2: ModelRef


class UWApiConfig(BaseModel):
    base_url: str = "https://api.unusualwhales.com"
    rate_limit_rpm: int = 120
    daily_limit: int = 15000
    retry_max: int = 3
    retry_backoff_base: int = 2


class HealthConfig(BaseModel):
    enabled: bool = True
    port: int = 8090


class StorageConfig(BaseModel):
    db_path: str = "data/signals.duckdb"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    file: str = "logs/scanner.log"
    rotation: str = "10MB"


class TokenRate(BaseModel):
    input_per_mtok: float
    output_per_mtok: float


class OpsConfig(BaseModel):
    daily_spend_cap_usd: float = 10.0
    token_rates: dict[str, TokenRate] = Field(default_factory=dict)


class _Secrets(BaseSettings):
    """Read secrets from environment variables using pydantic-settings."""
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    uw_api_key: str = ""
    anthropic_api_key: str = ""
    discord_webhook_url: str = ""


class AppConfig(BaseModel):
    scheduler: SchedulerConfig
    scoring: ScoringConfig
    models: ModelsConfig
    uw_api: UWApiConfig
    health: HealthConfig = HealthConfig()
    storage: StorageConfig = StorageConfig()
    logging: LoggingConfig = LoggingConfig()
    ops: OpsConfig = OpsConfig()

    # Secrets — populated from env vars via pydantic-settings, not YAML
    uw_api_key: str = ""
    anthropic_api_key: str = ""
    discord_webhook_url: str = ""


def load_config(config_path: Path | str = "config/config.yaml") -> AppConfig:
    """Load config from YAML file + env vars. Raises ValueError if secrets missing."""
    config_path = Path(config_path)
    with open(config_path) as f:
        data = yaml.safe_load(f)

    cfg = AppConfig(**data)

    # Inject secrets from environment (via pydantic-settings)
    secrets = _Secrets()
    cfg.uw_api_key = secrets.uw_api_key
    cfg.anthropic_api_key = secrets.anthropic_api_key
    cfg.discord_webhook_url = secrets.discord_webhook_url

    # Validate secrets
    missing = []
    if not cfg.uw_api_key:
        missing.append("UW_API_KEY")
    if not cfg.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if not cfg.discord_webhook_url:
        missing.append("DISCORD_WEBHOOK_URL")
    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Set them in your environment or .env file."
        )

    # Validate DISCORD_WEBHOOK_URL is a valid URL
    if not cfg.discord_webhook_url.startswith("https://"):
        raise ValueError(
            f"DISCORD_WEBHOOK_URL must be a valid HTTPS URL, got: {cfg.discord_webhook_url!r}"
        )

    # Validate storage path writability
    db_parent = Path(cfg.storage.db_path).parent
    if not db_parent.exists():
        db_parent.mkdir(parents=True, exist_ok=True)
    # Verify we can actually write to the directory
    test_file = db_parent / ".write_test"
    try:
        test_file.touch()
        test_file.unlink()
    except OSError as e:
        raise ValueError(f"DuckDB path parent is not writable: {db_parent}: {e}") from e

    return cfg
