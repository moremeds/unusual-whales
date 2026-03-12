from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from uw_flow_scanner.alerting.discord import DiscordAlerter, build_embed
from uw_flow_scanner.core.schemas import FlowEvent, Tier2Result


@pytest.fixture
def alerter() -> DiscordAlerter:
    return DiscordAlerter(webhook_url="https://discord.com/api/webhooks/123/abc")


@pytest.fixture
def tier2_result() -> Tier2Result:
    return Tier2Result(
        score=88,
        direction="bullish",
        confidence=0.85,
        conviction_factors=["$1.25M premium sweep", "5200 vol vs 12K OI", "OTM call"],
        reasoning="Strong bullish conviction on AAPL with large premium sweep targeting $185 strike.",
    )


@pytest.fixture
def flow_event() -> FlowEvent:
    return FlowEvent(
        uw_event_id="test-alert-123",
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


def test_build_embed(flow_event: FlowEvent, tier2_result: Tier2Result):
    """Embed contains all required fields."""
    embed = build_embed(flow_event, tier2_result)

    assert embed["title"] is not None
    assert "AAPL" in embed["title"]
    assert embed["color"] == 0x00FF00  # green for bullish
    assert any("Score" in f["name"] for f in embed["fields"])
    assert any("Direction" in f["name"] for f in embed["fields"])


@respx.mock
@pytest.mark.asyncio
async def test_send_alert_success(
    alerter: DiscordAlerter, flow_event: FlowEvent, tier2_result: Tier2Result
):
    """Alert is sent successfully via webhook."""
    respx.post("https://discord.com/api/webhooks/123/abc").mock(
        return_value=httpx.Response(204)
    )

    success = await alerter.send_alert(flow_event, tier2_result)
    assert success is True


@respx.mock
@pytest.mark.asyncio
async def test_send_alert_failure_returns_false(
    alerter: DiscordAlerter, flow_event: FlowEvent, tier2_result: Tier2Result
):
    """Alert failure returns False (does not raise)."""
    respx.post("https://discord.com/api/webhooks/123/abc").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    success = await alerter.send_alert(flow_event, tier2_result)
    assert success is False
