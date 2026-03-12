from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import duckdb
import pytest

SAMPLE_FLOW_EVENT: dict[str, Any] = {
    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "ticker_symbol": "AAPL",
    "sentiment": "bullish",
    "total_premium": "1250000.00",
    "strike_price": "185.00",
    "expires": "2026-04-17",
    "volume": "5200",
    "open_interest": "12000",
    "underlying_price": "182.50",
    "option_type": "call",
    "flow_type": "sweep",
    "executed_at": "2026-03-12T14:30:00Z",
}


@pytest.fixture
def sample_flow_event() -> dict[str, Any]:
    return {**SAMPLE_FLOW_EVENT, "id": str(uuid.uuid4())}


@pytest.fixture
def sample_flow_batch() -> list[dict[str, Any]]:
    events = []
    for i in range(5):
        event = {**SAMPLE_FLOW_EVENT, "id": str(uuid.uuid4())}
        event["ticker_symbol"] = ["AAPL", "TSLA", "SPY", "NVDA", "MSFT"][i]
        event["total_premium"] = str(500000 + i * 250000)
        events.append(event)
    return events


@pytest.fixture
def tmp_db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    db_path = tmp_path / "test.duckdb"
    con = duckdb.connect(str(db_path))
    yield con
    con.close()
