from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import duckdb
import pytest

from uw_flow_scanner.core.db import SignalDB
from uw_flow_scanner.core.schemas import FlowEvent


@pytest.fixture
def signal_db(tmp_path) -> SignalDB:
    db_path = str(tmp_path / "test.duckdb")
    db = SignalDB(db_path)
    db.init_tables()
    return db


def _make_event(ticker: str = "AAPL", uw_id: str | None = None) -> FlowEvent:
    return FlowEvent(
        uw_event_id=uw_id or str(uuid.uuid4()),
        tape_time=datetime.now(timezone.utc),
        ticker=ticker,
        underlying_price=Decimal("182.50"),
        flow_type="sweep",
        side="call",
        sentiment="bullish",
        premium=Decimal("1250000"),
        strike=Decimal("185.00"),
        expiry=date(2026, 4, 17),
        volume=5200,
        open_interest=12000,
        raw_json={"id": "test"},
    )


def test_init_creates_tables(signal_db: SignalDB):
    """init_tables creates flow_events and signal_scores."""
    tables = signal_db.con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    ).fetchall()
    table_names = {t[0] for t in tables}
    assert "flow_events" in table_names
    assert "signal_scores" in table_names


def test_insert_flow_event_returns_true_for_new(signal_db: SignalDB):
    """insert_flow_event returns True when the row is new."""
    event = _make_event()
    is_new = signal_db.insert_flow_event(event)
    assert is_new is True

    rows = signal_db.con.execute(
        "SELECT ticker, premium FROM flow_events WHERE uw_event_id = ?",
        [event.uw_event_id],
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "AAPL"


def test_insert_duplicate_event_returns_false(signal_db: SignalDB):
    """Duplicate uw_event_id returns False and is not re-inserted."""
    event = _make_event(uw_id="same-id")
    first = signal_db.insert_flow_event(event)
    second = signal_db.insert_flow_event(event)
    assert first is True
    assert second is False

    count = signal_db.con.execute(
        "SELECT COUNT(*) FROM flow_events WHERE uw_event_id = 'same-id'"
    ).fetchone()[0]
    assert count == 1


def test_insert_signal_score(signal_db: SignalDB):
    """Insert a signal score linked to a flow event."""
    event = _make_event()
    signal_db.insert_flow_event(event)

    score_id = signal_db.insert_signal_score(
        uw_event_id=event.uw_event_id,
        flow_event_id=event.id,
        tier=1,
        model_used="claude-haiku-4-5",
        prompt_version="v1.0.0",
        score=82,
        direction="bullish",
        confidence=None,
        reasoning="High premium sweep on AAPL",
        raw_output={"score": 82, "direction": "bullish"},
    )
    assert score_id is not None

    row = signal_db.con.execute(
        "SELECT score, direction, alert_status FROM signal_scores WHERE id = ?",
        [str(score_id)],
    ).fetchone()
    assert row[0] == 82
    assert row[1] == "bullish"
    assert row[2] == "pending"


def test_update_alert_status(signal_db: SignalDB):
    """Update alert_status after Discord delivery."""
    event = _make_event()
    signal_db.insert_flow_event(event)
    score_id = signal_db.insert_signal_score(
        uw_event_id=event.uw_event_id,
        flow_event_id=event.id,
        tier=2,
        model_used="claude-sonnet-4-6",
        prompt_version="v1.0.0",
        score=88,
        direction="bullish",
        confidence=0.85,
        reasoning="test",
        raw_output={},
    )

    signal_db.update_alert_status(score_id, "sent")

    status = signal_db.con.execute(
        "SELECT alert_status FROM signal_scores WHERE id = ?",
        [str(score_id)],
    ).fetchone()[0]
    assert status == "sent"


def test_get_last_alert_time(signal_db: SignalDB):
    """Returns the last alert time for a ticker (for cooldown)."""
    event = _make_event(ticker="TSLA")
    signal_db.insert_flow_event(event)
    signal_db.insert_signal_score(
        uw_event_id=event.uw_event_id,
        flow_event_id=event.id,
        tier=2,
        model_used="claude-sonnet-4-6",
        prompt_version="v1.0.0",
        score=90,
        direction="bullish",
        confidence=0.9,
        reasoning="test",
        raw_output={},
    )
    # Mark as sent
    score_id = signal_db.con.execute(
        "SELECT id FROM signal_scores WHERE flow_event_id = ?", [str(event.id)]
    ).fetchone()[0]
    signal_db.update_alert_status(uuid.UUID(score_id), "sent")

    last = signal_db.get_last_alert_time("TSLA")
    assert last is not None

    none_result = signal_db.get_last_alert_time("NOPE")
    assert none_result is None


@pytest.mark.asyncio
async def test_async_insert(signal_db: SignalDB):
    """Async wrapper delegates to thread."""
    event = _make_event()
    is_new = await signal_db.async_insert_flow_event(event)
    assert is_new is True

    count = signal_db.con.execute("SELECT COUNT(*) FROM flow_events").fetchone()[0]
    assert count == 1
