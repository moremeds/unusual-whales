from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from uw_flow_scanner.core.config import load_config
from uw_flow_scanner.core.schemas import Tier1Result, Tier2Result
from uw_flow_scanner.main import Scanner


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Load config with test overrides."""
    yaml_content = """
scheduler:
  market_hours_only: false
  poll_interval_seconds: 1
scoring:
  score_threshold: 75
  tier1_concurrency: 2
  tier1_timeout_seconds: 5
  tier2_timeout_seconds: 30
  alert_cooldown_seconds: 0
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
  retry_max: 1
  retry_backoff_base: 1
health:
  enabled: false
  port: 0
storage:
  db_path: {db_path}
logging:
  level: DEBUG
  format: console
ops:
  daily_spend_cap_usd: 10.0
  token_rates: {{}}
""".format(db_path=str(tmp_path / "test.duckdb"))

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content)

    monkeypatch.setenv("UW_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc")

    return load_config(config_file)


@respx.mock
@pytest.mark.asyncio
async def test_full_cycle_poll_score_alert(cfg, sample_flow_event: dict):
    """Integration: poll -> tier1 -> tier2 -> discord -> db."""
    # Mock UW API
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": [sample_flow_event]},
            headers={"x-uw-daily-req-count": "1", "x-uw-req-per-minute-remaining": "119"},
        )
    )

    # Mock Discord
    respx.post("https://discord.com/api/webhooks/123/abc").mock(
        return_value=httpx.Response(204)
    )

    scanner = Scanner(cfg)

    # Mock LLM scorer
    t1 = Tier1Result(score=90, direction="bullish", reasoning="High premium sweep")
    t2 = Tier2Result(
        score=88,
        direction="bullish",
        confidence=0.85,
        conviction_factors=["Large premium"],
        reasoning="Strong signal",
    )
    scanner.scorer.score_tier1 = AsyncMock(return_value=t1)
    scanner.scorer.score_tier2 = AsyncMock(return_value=t2)

    # Run one cycle
    await scanner.run_cycle()

    # Verify event was stored
    count = scanner.db.con.execute("SELECT COUNT(*) FROM flow_events").fetchone()[0]
    assert count == 1

    # Verify both tier scores were stored
    scores = scanner.db.con.execute(
        "SELECT tier, score, alert_status FROM signal_scores ORDER BY tier"
    ).fetchall()
    assert len(scores) == 2
    assert scores[0] == (1, 90, "pending")  # Tier 1 — no alert
    assert scores[1] == (2, 88, "sent")  # Tier 2 — alert sent

    # Verify health was recorded even though there were events
    assert scanner.health_state.last_poll is not None
    assert scanner.health_state.status == "ok"

    # Cleanup
    await scanner.shutdown()


@respx.mock
@pytest.mark.asyncio
async def test_low_score_skips_tier2(cfg, sample_flow_event: dict):
    """Events scoring below threshold don't get Tier 2 or Discord alert."""
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": [sample_flow_event]},
            headers={"x-uw-daily-req-count": "1", "x-uw-req-per-minute-remaining": "119"},
        )
    )

    scanner = Scanner(cfg)

    # Tier 1 returns low score
    t1 = Tier1Result(score=40, direction="neutral", reasoning="Low conviction")
    scanner.scorer.score_tier1 = AsyncMock(return_value=t1)
    scanner.scorer.score_tier2 = AsyncMock()  # should NOT be called

    await scanner.run_cycle()

    # Only Tier 1 score stored
    scores = scanner.db.con.execute("SELECT tier FROM signal_scores").fetchall()
    assert len(scores) == 1
    assert scores[0][0] == 1

    # Tier 2 was never called
    scanner.scorer.score_tier2.assert_not_called()

    # Verify health recorded even for low-score cycles
    assert scanner.health_state.last_poll is not None

    await scanner.shutdown()


@respx.mock
@pytest.mark.asyncio
async def test_empty_poll_records_health(cfg):
    """An empty poll still updates health state."""
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": []},
            headers={"x-uw-daily-req-count": "1", "x-uw-req-per-minute-remaining": "119"},
        )
    )

    scanner = Scanner(cfg)
    await scanner.run_cycle()

    # Health must be updated even on empty poll (FIX 8)
    assert scanner.health_state.last_poll is not None
    assert scanner.health_state.status == "ok"

    await scanner.shutdown()


@respx.mock
@pytest.mark.asyncio
async def test_duplicate_event_skipped(cfg, sample_flow_event: dict):
    """A duplicate uw_event_id (returned by the API twice) is not scored twice (FIX 1)."""
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": [sample_flow_event]},
            headers={"x-uw-daily-req-count": "1", "x-uw-req-per-minute-remaining": "119"},
        )
    )

    scanner = Scanner(cfg)
    t1 = Tier1Result(score=90, direction="bullish", reasoning="High premium sweep")
    scanner.scorer.score_tier1 = AsyncMock(return_value=t1)
    scanner.scorer.score_tier2 = AsyncMock(return_value=None)

    # First cycle — event is new
    await scanner.run_cycle()
    first_count = scanner.db.con.execute("SELECT COUNT(*) FROM flow_events").fetchone()[0]
    assert first_count == 1

    # Second cycle with same event — poller deduplicates via _seen_ids
    await scanner.run_cycle()
    second_count = scanner.db.con.execute("SELECT COUNT(*) FROM flow_events").fetchone()[0]
    assert second_count == 1  # still only 1 row

    await scanner.shutdown()
