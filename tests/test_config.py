from __future__ import annotations

import os
from pathlib import Path

import pytest

from uw_flow_scanner.core.config import AppConfig, load_config


def test_load_config_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Config loads from YAML and env vars."""
    yaml_content = """
scheduler:
  market_hours_only: true
  poll_interval_seconds: 30
scoring:
  score_threshold: 75
  tier1_concurrency: 10
  tier1_timeout_seconds: 5
  tier2_timeout_seconds: 30
  alert_cooldown_seconds: 600
models:
  tier1:
    provider: anthropic
    model: claude-haiku-4-5
  tier2:
    provider: anthropic
    model: claude-sonnet-4-6
uw_api:
  base_url: https://api.unusualwhales.com
  rate_limit_rpm: 120
  daily_limit: 15000
  retry_max: 3
  retry_backoff_base: 2
health:
  enabled: true
  port: 8090
storage:
  db_path: data/signals.duckdb
logging:
  level: INFO
  format: json
  file: logs/scanner.log
  rotation: 10MB
ops:
  daily_spend_cap_usd: 10.0
  token_rates:
    claude-haiku-4-5:
      input_per_mtok: 0.25
      output_per_mtok: 1.25
    claude-sonnet-4-6:
      input_per_mtok: 3.0
      output_per_mtok: 15.0
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content)

    monkeypatch.setenv("UW_API_KEY", "test-uw-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc")

    cfg = load_config(config_file)

    assert cfg.scoring.score_threshold == 75
    assert cfg.models.tier1.model == "claude-haiku-4-5"
    assert cfg.uw_api_key == "test-uw-key"
    assert cfg.anthropic_api_key == "test-anthropic-key"
    assert cfg.discord_webhook_url == "https://discord.com/api/webhooks/123/abc"
    assert cfg.ops.daily_spend_cap_usd == 10.0
    assert cfg.ops.token_rates["claude-haiku-4-5"].input_per_mtok == 0.25


def test_config_fails_without_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Config validation fails if required secrets are missing."""
    yaml_content = """
scheduler:
  market_hours_only: true
  poll_interval_seconds: 30
scoring:
  score_threshold: 75
  tier1_concurrency: 10
  tier1_timeout_seconds: 5
  tier2_timeout_seconds: 30
  alert_cooldown_seconds: 600
models:
  tier1:
    provider: anthropic
    model: claude-haiku-4-5
  tier2:
    provider: anthropic
    model: claude-sonnet-4-6
uw_api:
  base_url: https://api.unusualwhales.com
  rate_limit_rpm: 120
  daily_limit: 15000
  retry_max: 3
  retry_backoff_base: 2
health:
  enabled: true
  port: 8090
storage:
  db_path: data/signals.duckdb
logging:
  level: INFO
  format: json
  file: logs/scanner.log
  rotation: 10MB
ops:
  daily_spend_cap_usd: 10.0
  token_rates: {}
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content)

    # Clear all relevant env vars
    monkeypatch.delenv("UW_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    with pytest.raises(ValueError, match="UW_API_KEY"):
        load_config(config_file)


def test_config_score_threshold_bounds():
    """Score threshold must be 0-100."""
    from uw_flow_scanner.core.config import ScoringConfig
    with pytest.raises(Exception):
        ScoringConfig(
            score_threshold=150,
            tier1_concurrency=10,
            tier1_timeout_seconds=5,
            tier2_timeout_seconds=30,
            alert_cooldown_seconds=600,
        )
