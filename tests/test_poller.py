from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from uw_flow_scanner.ingestion.poller import RateLimitState, UWPoller
from uw_flow_scanner.core.schemas import FlowEvent


@pytest.fixture
def poller() -> UWPoller:
    return UWPoller(
        base_url="https://api.unusualwhales.com",
        api_key="test-key",
        rate_limit_rpm=120,
        daily_limit=15000,
        retry_max=3,
        retry_backoff_base=2,
    )


@respx.mock
@pytest.mark.asyncio
async def test_poll_returns_flow_events(poller: UWPoller, sample_flow_batch: list[dict]):
    """Poll fetches and parses flow events from UW API."""
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": sample_flow_batch},
            headers={
                "x-uw-daily-req-count": "100",
                "x-uw-req-per-minute-remaining": "119",
            },
        )
    )

    events = await poller.poll()
    assert len(events) == 5
    assert all(isinstance(e, FlowEvent) for e in events)
    assert events[0].ticker == "AAPL"


@respx.mock
@pytest.mark.asyncio
async def test_poll_deduplicates_by_uw_event_id(poller: UWPoller, sample_flow_event: dict):
    """Events already seen (by uw_event_id) are filtered out."""
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": [sample_flow_event]},
            headers={
                "x-uw-daily-req-count": "100",
                "x-uw-req-per-minute-remaining": "119",
            },
        )
    )

    events1 = await poller.poll()
    assert len(events1) == 1

    # Second poll with same event ID → deduplicated
    events2 = await poller.poll()
    assert len(events2) == 0


@respx.mock
@pytest.mark.asyncio
async def test_poll_updates_rate_limit_state(poller: UWPoller, sample_flow_batch: list[dict]):
    """Rate limit state is updated from response headers."""
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": sample_flow_batch},
            headers={
                "x-uw-daily-req-count": "500",
                "x-uw-req-per-minute-remaining": "115",
            },
        )
    )

    await poller.poll()
    assert poller.rate_state.daily_count == 500
    assert poller.rate_state.minute_remaining == 115


@respx.mock
@pytest.mark.asyncio
async def test_poll_retries_on_server_error(poller: UWPoller, sample_flow_batch: list[dict]):
    """Retries on 5xx with exponential backoff, succeeds on retry."""
    route = respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts")
    route.side_effect = [
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(
            200,
            json={"data": sample_flow_batch},
            headers={
                "x-uw-daily-req-count": "100",
                "x-uw-req-per-minute-remaining": "119",
            },
        ),
    ]

    events = await poller.poll()
    assert len(events) == 5
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_poll_returns_empty_after_max_retries(poller: UWPoller):
    """Returns empty list after exhausting retries."""
    respx.get("https://api.unusualwhales.com/api/option-trades/flow-alerts").mock(
        return_value=httpx.Response(500, text="Server Error"),
    )

    events = await poller.poll()
    assert events == []


def test_watermark_updates(poller: UWPoller):
    """Watermark tracks the latest tape_time seen."""
    assert poller.watermark is None

    now = datetime.now(timezone.utc)
    poller.update_watermark(now)
    assert poller.watermark == now

    earlier = now - timedelta(minutes=5)
    poller.update_watermark(earlier)
    assert poller.watermark == now  # should not go backwards
