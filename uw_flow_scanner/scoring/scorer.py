from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TypeVar

import structlog
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from uw_flow_scanner.scoring.prompts import (
    PROMPT_VERSION,
    TIER1_SYSTEM,
    TIER2_SYSTEM,
    format_tier1_prompt,
    format_tier2_prompt,
)
from uw_flow_scanner.core.schemas import FlowEvent, Tier1Result, Tier2Result

logger = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)


class SpendTracker:
    def __init__(self, daily_cap_usd: float, token_rates: dict):
        self.daily_cap_usd = daily_cap_usd
        self.token_rates = token_rates
        self.daily_spend_usd: float = 0.0
        # FIX 6: Use UTC date for daily reset
        self._reset_date: date = datetime.now(timezone.utc).date()
        self._budget_alert_sent: bool = False

    def _check_reset(self) -> None:
        # FIX 6: Use UTC date, not local date.today()
        today = datetime.now(timezone.utc).date()
        if today != self._reset_date:
            self.daily_spend_usd = 0.0
            self._reset_date = today
            self._budget_alert_sent = False

    def record_usage(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self._check_reset()
        rates = self.token_rates.get(model)
        if not rates:
            return
        input_cost = (input_tokens / 1_000_000) * rates["input_per_mtok"]
        output_cost = (output_tokens / 1_000_000) * rates["output_per_mtok"]
        self.daily_spend_usd += input_cost + output_cost

    @property
    def is_budget_exhausted(self) -> bool:
        self._check_reset()
        return self.daily_spend_usd >= self.daily_cap_usd


def _event_to_prompt_data(event: FlowEvent) -> dict:
    return {
        "ticker": event.ticker,
        "side": event.side,
        "sentiment": event.sentiment,
        "flow_type": event.flow_type,
        "premium": float(event.premium),
        "strike": float(event.strike),
        "expiry": event.expiry.isoformat(),
        "volume": event.volume,
        "open_interest": event.open_interest,
        "underlying_price": float(event.underlying_price),
    }


def _pydantic_to_tool(name: str, description: str, model_cls: type[BaseModel]) -> dict:
    """Convert a Pydantic model to an Anthropic tool definition."""
    schema = model_cls.model_json_schema()
    # FIX 2: Remove title and $defs — Tier1Result/Tier2Result use Literal so no $defs exist,
    # but clean up defensively to ensure flat schema.
    schema.pop("title", None)
    schema.pop("$defs", None)
    return {
        "name": name,
        "description": description,
        "input_schema": schema,
    }


TIER1_TOOL = _pydantic_to_tool(
    "score_flow_event",
    "Score an options flow event for directional conviction.",
    Tier1Result,
)

TIER2_TOOL = _pydantic_to_tool(
    "analyze_flow_event",
    "Provide detailed directional analysis of an options flow event.",
    Tier2Result,
)


def _extract_tool_result(response, model_cls: type[T]) -> T | None:
    """Extract structured data from an Anthropic tool_use response block."""
    for block in response.content:
        if block.type == "tool_use":
            return model_cls.model_validate(block.input)
    return None


class LLMScorer:
    def __init__(
        self,
        client: AsyncAnthropic,
        tier1_model: str,
        tier2_model: str,
        tier1_timeout: int,
        tier2_timeout: int,
        spend_tracker: SpendTracker,
    ):
        self.client = client
        self.tier1_model = tier1_model
        self.tier2_model = tier2_model
        self.tier1_timeout = tier1_timeout
        self.tier2_timeout = tier2_timeout
        self.spend_tracker = spend_tracker
        self.prompt_version = PROMPT_VERSION

    async def score_tier1(self, event: FlowEvent) -> Tier1Result | None:
        """Fast scan with Haiku. Returns None on failure."""
        prompt_data = _event_to_prompt_data(event)
        user_msg = format_tier1_prompt(prompt_data)

        try:
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model=self.tier1_model,
                    max_tokens=256,
                    system=TIER1_SYSTEM,
                    tools=[TIER1_TOOL],
                    tool_choice={"type": "tool", "name": "score_flow_event"},
                    messages=[{"role": "user", "content": user_msg}],
                ),
                timeout=self.tier1_timeout,
            )
            self.spend_tracker.record_usage(
                self.tier1_model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return _extract_tool_result(response, Tier1Result)
        except asyncio.TimeoutError:
            logger.warning("Tier 1 timeout", ticker=event.ticker)
            return None
        except Exception as e:
            logger.error("Tier 1 scoring failed", ticker=event.ticker, error=str(e))
            return None

    async def score_tier2(self, event: FlowEvent) -> Tier2Result | None:
        """Full analysis with Sonnet. Returns None on failure or budget exhaustion."""
        if self.spend_tracker.is_budget_exhausted:
            logger.warning("Budget exhausted, skipping Tier 2", ticker=event.ticker)
            return None

        prompt_data = _event_to_prompt_data(event)
        user_msg = format_tier2_prompt(prompt_data)

        try:
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model=self.tier2_model,
                    max_tokens=1024,
                    system=TIER2_SYSTEM,
                    tools=[TIER2_TOOL],
                    tool_choice={"type": "tool", "name": "analyze_flow_event"},
                    messages=[{"role": "user", "content": user_msg}],
                ),
                timeout=self.tier2_timeout,
            )
            self.spend_tracker.record_usage(
                self.tier2_model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return _extract_tool_result(response, Tier2Result)
        except asyncio.TimeoutError:
            logger.warning("Tier 2 timeout", ticker=event.ticker)
            return None
        except Exception as e:
            logger.error("Tier 2 scoring failed", ticker=event.ticker, error=str(e))
            return None
