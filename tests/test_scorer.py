from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from uw_flow_scanner.core.schemas import FlowEvent, Tier1Result, Tier2Result
from uw_flow_scanner.scoring.scorer import LLMScorer, SpendTracker


@pytest.fixture
def spend_tracker() -> SpendTracker:
    return SpendTracker(
        daily_cap_usd=10.0,
        token_rates={
            "claude-haiku-4-5": {"input_per_mtok": 0.25, "output_per_mtok": 1.25},
            "claude-sonnet-4-6": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
        },
    )


@pytest.fixture
def mock_event() -> FlowEvent:
    return FlowEvent(
        uw_event_id="test-123",
        tape_time=datetime.now(timezone.utc),
        ticker="AAPL",
        underlying_price=Decimal("182.50"),
        flow_type="sweep",
        side="call",
        sentiment="bullish",
        premium=Decimal("1250000"),
        strike=Decimal("185.00"),
        expiry=date(2026, 4, 17),
        volume=5200,
        open_interest=12000,
        raw_json={},
    )


def test_spend_tracker_accumulates(spend_tracker: SpendTracker):
    """SpendTracker accumulates token costs."""
    spend_tracker.record_usage("claude-haiku-4-5", input_tokens=200, output_tokens=50)
    assert spend_tracker.daily_spend_usd > 0
    assert not spend_tracker.is_budget_exhausted


def test_spend_tracker_budget_exhaustion(spend_tracker: SpendTracker):
    """Budget is exhausted when spend exceeds cap."""
    # Sonnet is $3/Mtok input, $15/Mtok output
    # 1M input = $3, 1M output = $15 → total $18 > $10 cap
    spend_tracker.record_usage("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000)
    assert spend_tracker.is_budget_exhausted


def test_spend_tracker_resets_daily(spend_tracker: SpendTracker):
    """Spend resets when date changes (uses UTC)."""
    spend_tracker.record_usage("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000)
    assert spend_tracker.is_budget_exhausted

    # Simulate date change — subtract 1 day to guarantee a different date
    spend_tracker._reset_date -= timedelta(days=1)
    spend_tracker._check_reset()
    assert not spend_tracker.is_budget_exhausted


@pytest.mark.asyncio
async def test_scorer_tier1(mock_event: FlowEvent):
    """Tier 1 scoring returns Tier1Result via tool_use structured output."""
    mock_tool_block = MagicMock()
    mock_tool_block.type = "tool_use"
    mock_tool_block.input = {
        "score": 82, "direction": "bullish", "reasoning": "Large premium sweep",
    }

    mock_response = MagicMock()
    mock_response.content = [mock_tool_block]
    mock_response.usage = MagicMock(input_tokens=200, output_tokens=50)

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    scorer = LLMScorer(
        client=mock_client,
        tier1_model="claude-haiku-4-5",
        tier2_model="claude-sonnet-4-6",
        tier1_timeout=5,
        tier2_timeout=30,
        spend_tracker=SpendTracker(daily_cap_usd=10.0, token_rates={}),
    )

    result = await scorer.score_tier1(mock_event)
    assert isinstance(result, Tier1Result)
    assert result.score == 82


@pytest.mark.asyncio
async def test_scorer_tier2(mock_event: FlowEvent):
    """Tier 2 scoring returns Tier2Result via tool_use."""
    mock_tool_block = MagicMock()
    mock_tool_block.type = "tool_use"
    mock_tool_block.input = {
        "score": 88,
        "direction": "bullish",
        "confidence": 0.85,
        "conviction_factors": ["Large premium", "Sweep"],
        "reasoning": "Strong conviction",
    }

    mock_response = MagicMock()
    mock_response.content = [mock_tool_block]
    mock_response.usage = MagicMock(input_tokens=800, output_tokens=200)

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    scorer = LLMScorer(
        client=mock_client,
        tier1_model="claude-haiku-4-5",
        tier2_model="claude-sonnet-4-6",
        tier1_timeout=5,
        tier2_timeout=30,
        spend_tracker=SpendTracker(daily_cap_usd=10.0, token_rates={}),
    )

    result = await scorer.score_tier2(mock_event)
    assert isinstance(result, Tier2Result)
    assert result.confidence == 0.85


@pytest.mark.asyncio
async def test_scorer_skips_tier2_when_budget_exhausted(mock_event: FlowEvent):
    """Tier 2 returns None when budget is exhausted."""
    tracker = SpendTracker(daily_cap_usd=0.001, token_rates={
        "claude-sonnet-4-6": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
    })
    tracker.record_usage("claude-sonnet-4-6", input_tokens=1000, output_tokens=1000)

    scorer = LLMScorer(
        client=MagicMock(),
        tier1_model="claude-haiku-4-5",
        tier2_model="claude-sonnet-4-6",
        tier1_timeout=5,
        tier2_timeout=30,
        spend_tracker=tracker,
    )

    result = await scorer.score_tier2(mock_event)
    assert result is None
