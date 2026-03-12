from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class FlowEvent(BaseModel):
    """Domain model for a single UW flow event."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    uw_event_id: str
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tape_time: datetime
    ticker: str
    underlying_price: Decimal
    flow_type: str
    side: str  # call / put
    sentiment: str  # bullish / bearish / neutral
    premium: Decimal
    strike: Decimal
    expiry: date
    volume: int
    open_interest: int
    raw_json: dict[str, Any]


# FIX 2: Use Literal instead of Direction enum to ensure flat JSON schema (no $defs/$ref)
DirectionLiteral = Literal["bullish", "bearish", "neutral"]


class Tier1Result(BaseModel):
    """Structured output from Tier 1 (Haiku) fast scan."""

    score: int = Field(ge=0, le=100, description="Conviction score 0-100")
    direction: DirectionLiteral = Field(description="Predicted direction")
    reasoning: str = Field(description="Brief rationale for the score")


class Tier2Result(BaseModel):
    """Structured output from Tier 2 (Sonnet) full analysis."""

    score: int = Field(ge=0, le=100, description="Conviction score 0-100")
    direction: DirectionLiteral = Field(description="Predicted direction")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence level 0.0-1.0")
    conviction_factors: list[str] = Field(description="Key factors driving conviction")
    reasoning: str = Field(description="Detailed analysis rationale")


def parse_flow_event(raw: dict[str, Any]) -> FlowEvent:
    """Parse a raw UW API flow-alerts record into a FlowEvent."""
    return FlowEvent(
        uw_event_id=raw["id"],
        tape_time=datetime.fromisoformat(raw["executed_at"].replace("Z", "+00:00")),
        ticker=raw["ticker_symbol"],
        underlying_price=Decimal(raw["underlying_price"]),
        flow_type=raw["flow_type"],
        side=raw["option_type"],
        sentiment=raw["sentiment"],
        premium=Decimal(raw["total_premium"]),
        strike=Decimal(raw["strike_price"]),
        expiry=date.fromisoformat(raw["expires"]),
        volume=int(raw["volume"]),
        open_interest=int(raw["open_interest"]),
        raw_json=raw,
    )
