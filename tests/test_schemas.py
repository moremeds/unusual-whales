from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from uw_flow_scanner.core.schemas import FlowEvent, Tier1Result, Tier2Result, parse_flow_event


def test_parse_flow_event_from_api(sample_flow_event: dict):
    """Parse a UW API flow event into our domain model."""
    event = parse_flow_event(sample_flow_event)

    assert event.uw_event_id == sample_flow_event["id"]
    assert event.ticker == "AAPL"
    assert event.side == "call"
    assert event.sentiment == "bullish"
    assert event.premium == Decimal("1250000.00")
    assert event.strike == Decimal("185.00")
    assert event.underlying_price == Decimal("182.50")
    assert event.volume == 5200
    assert event.open_interest == 12000
    assert event.flow_type == "sweep"
    assert event.expiry == date(2026, 4, 17)
    assert event.raw_json == sample_flow_event


def test_parse_flow_event_handles_string_numbers(sample_flow_event: dict):
    """All numeric fields from UW API come as strings — parser converts them."""
    event = parse_flow_event(sample_flow_event)
    assert isinstance(event.premium, Decimal)
    assert isinstance(event.volume, int)


def test_tier1_result_schema():
    """Tier1Result validates score range and direction literal."""
    result = Tier1Result(score=85, direction="bullish", reasoning="High premium sweep")
    assert result.score == 85
    assert result.direction == "bullish"


def test_tier1_result_rejects_invalid_score():
    """Score must be 0-100."""
    with pytest.raises(Exception):
        Tier1Result(score=150, direction="bullish", reasoning="test")


def test_tier1_result_rejects_invalid_direction():
    """Direction must be bullish/bearish/neutral."""
    with pytest.raises(Exception):
        Tier1Result(score=50, direction="sideways", reasoning="test")


def test_tier2_result_schema():
    """Tier2Result includes confidence and conviction_factors."""
    result = Tier2Result(
        score=88,
        direction="bullish",
        confidence=0.85,
        conviction_factors=["Large premium", "Sweep order", "OTM call"],
        reasoning="Strong conviction bullish flow",
    )
    assert result.confidence == 0.85
    assert len(result.conviction_factors) == 3


def test_tier1_tool_schema_has_no_defs():
    """Tier1Result tool schema must not contain $defs or $ref (flat schema required)."""
    schema = Tier1Result.model_json_schema()
    assert "$defs" not in schema
    assert "$ref" not in str(schema)


def test_tier2_tool_schema_has_no_defs():
    """Tier2Result tool schema must not contain $defs or $ref (flat schema required)."""
    schema = Tier2Result.model_json_schema()
    assert "$defs" not in schema
    assert "$ref" not in str(schema)
