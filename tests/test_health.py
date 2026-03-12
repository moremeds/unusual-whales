from __future__ import annotations

import asyncio
import json

import pytest

from uw_flow_scanner.health.server import HealthServer, HealthState


@pytest.fixture
def health_state() -> HealthState:
    return HealthState()


def test_health_state_default(health_state: HealthState):
    """Default health state is unhealthy (no polls yet)."""
    data = health_state.to_dict()
    assert data["status"] == "starting"
    assert data["last_poll"] is None


def test_health_state_after_poll(health_state: HealthState):
    """Health state updates after successful poll."""
    health_state.record_poll(events_scored=10, uw_daily_remaining=14990)
    data = health_state.to_dict()
    assert data["status"] == "ok"
    assert data["last_poll"] is not None
    assert data["uw_daily_remaining"] == 14990


@pytest.mark.asyncio
async def test_health_server_responds():
    """Health server returns JSON on TCP connection."""
    state = HealthState()
    state.record_poll(events_scored=5, uw_daily_remaining=14000)

    server = HealthServer(state, port=0)  # port=0 picks a random free port
    await server.start()

    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(b"GET /health HTTP/1.0\r\n\r\n")
    await writer.drain()

    response = await reader.read(4096)
    writer.close()
    await writer.wait_closed()
    await server.stop()

    body = response.decode().split("\r\n\r\n", 1)[1]
    data = json.loads(body)
    assert data["status"] == "ok"
