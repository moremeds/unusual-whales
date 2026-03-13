from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import duckdb
import structlog

from uw_flow_scanner.core.schemas import FlowEvent

logger = structlog.get_logger()

FLOW_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS flow_events (
    id VARCHAR PRIMARY KEY,
    uw_event_id VARCHAR UNIQUE,
    ingested_at TIMESTAMPTZ,
    tape_time TIMESTAMPTZ,
    ticker VARCHAR,
    underlying_price DECIMAL,
    flow_type VARCHAR,
    side VARCHAR,
    sentiment VARCHAR,
    premium DECIMAL,
    strike DECIMAL,
    expiry DATE,
    volume INTEGER,
    open_interest INTEGER,
    raw_json JSON
)
"""

SIGNAL_SCORES_DDL = """
CREATE TABLE IF NOT EXISTS signal_scores (
    id VARCHAR PRIMARY KEY,
    uw_event_id VARCHAR,
    flow_event_id VARCHAR,
    tier INTEGER,
    model_used VARCHAR,
    prompt_version VARCHAR,
    score INTEGER,
    direction VARCHAR,
    confidence DECIMAL,
    reasoning TEXT,
    raw_output JSON,
    alert_key VARCHAR UNIQUE,
    alert_status VARCHAR DEFAULT 'pending',
    created_at TIMESTAMPTZ
)
"""

INDEXES_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_fe_ticker_tape ON flow_events (ticker, tape_time)",
    "CREATE INDEX IF NOT EXISTS idx_fe_ingested ON flow_events (ingested_at)",
    "CREATE INDEX IF NOT EXISTS idx_ss_tier_score ON signal_scores (tier, score)",
    "CREATE INDEX IF NOT EXISTS idx_ss_flow_event ON signal_scores (flow_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_ss_alert ON signal_scores (alert_status, created_at)",
]


def _make_alert_key(uw_event_id: str, tier: int, prompt_version: str) -> str:
    # FIX 1: Use uw_event_id (stable API ID) instead of transient event.id
    raw = f"{uw_event_id}:{tier}:{prompt_version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class SignalDB:
    def __init__(self, db_path: str = "data/signals.duckdb"):
        self.con = duckdb.connect(db_path)
        # FIX 5: Serialize all DuckDB access with asyncio.Lock to prevent concurrent corruption
        self._lock = asyncio.Lock()

    def init_tables(self) -> None:
        self.con.execute(FLOW_EVENTS_DDL)
        self.con.execute(SIGNAL_SCORES_DDL)
        for idx in INDEXES_DDL:
            self.con.execute(idx)

    def insert_flow_event(self, event: FlowEvent) -> bool:
        """Insert a flow event. Returns True if the row was new, False if duplicate."""
        # FIX 1: Use RETURNING to detect whether the INSERT actually wrote a row
        rows = self.con.execute(
            """
            INSERT INTO flow_events
            (id, uw_event_id, ingested_at, tape_time, ticker, underlying_price,
             flow_type, side, sentiment, premium, strike, expiry, volume,
             open_interest, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (uw_event_id) DO NOTHING
            RETURNING id
            """,
            [
                str(event.id),
                event.uw_event_id,
                event.ingested_at.isoformat(),
                event.tape_time.isoformat(),
                event.ticker,
                float(event.underlying_price),
                event.flow_type,
                event.side,
                event.sentiment,
                float(event.premium),
                float(event.strike),
                event.expiry.isoformat(),
                event.volume,
                event.open_interest,
                json.dumps(event.raw_json),
            ],
        ).fetchall()
        return len(rows) > 0

    def insert_signal_score(
        self,
        uw_event_id: str,
        flow_event_id: uuid.UUID,
        tier: int,
        model_used: str,
        prompt_version: str,
        score: int,
        direction: str,
        confidence: float | None,
        reasoning: str,
        raw_output: dict[str, Any],
    ) -> uuid.UUID | None:
        """Insert a signal score. Returns score_id if new, None if duplicate alert_key."""
        score_id = uuid.uuid4()
        alert_key = _make_alert_key(uw_event_id, tier, prompt_version)
        now = datetime.now(timezone.utc)

        rows = self.con.execute(
            """
            INSERT INTO signal_scores
            (id, uw_event_id, flow_event_id, tier, model_used, prompt_version, score,
             direction, confidence, reasoning, raw_output, alert_key,
             alert_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT (alert_key) DO NOTHING
            RETURNING id
            """,
            [
                str(score_id),
                uw_event_id,
                str(flow_event_id),
                tier,
                model_used,
                prompt_version,
                score,
                direction,
                confidence,
                reasoning,
                json.dumps(raw_output),
                alert_key,
                now.isoformat(),
            ],
        ).fetchall()
        if not rows:
            return None
        return score_id

    def update_alert_status(self, score_id: uuid.UUID, status: str) -> None:
        self.con.execute(
            "UPDATE signal_scores SET alert_status = ? WHERE id = ?",
            [status, str(score_id)],
        )

    def _ensure_utc(self, ts: Any) -> datetime:
        """Ensure a timestamp from DuckDB is timezone-aware (UTC)."""
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts

    def get_last_alert_time(self, ticker: str) -> datetime | None:
        """Get most recent sent alert time for a ticker (for cooldown)."""
        row = self.con.execute(
            """
            SELECT ss.created_at
            FROM signal_scores ss
            JOIN flow_events fe ON fe.id = ss.flow_event_id
            WHERE fe.ticker = ? AND ss.alert_status = 'sent'
            ORDER BY ss.created_at DESC
            LIMIT 1
            """,
            [ticker],
        ).fetchone()
        if row is None:
            return None
        return self._ensure_utc(row[0])

    def get_last_poll_watermark(self) -> datetime | None:
        """Get the most recent tape_time from flow_events (for watermark polling)."""
        row = self.con.execute(
            "SELECT MAX(tape_time) FROM flow_events"
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return self._ensure_utc(row[0])

    # Async wrappers — all serialized through self._lock (FIX 5)
    async def async_insert_flow_event(self, event: FlowEvent) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self.insert_flow_event, event)

    async def async_insert_signal_score(self, **kwargs: Any) -> uuid.UUID | None:
        async with self._lock:
            return await asyncio.to_thread(self.insert_signal_score, **kwargs)

    async def async_update_alert_status(self, score_id: uuid.UUID, status: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self.update_alert_status, score_id, status)

    async def async_get_last_alert_time(self, ticker: str) -> datetime | None:
        async with self._lock:
            return await asyncio.to_thread(self.get_last_alert_time, ticker)

    async def async_get_last_poll_watermark(self) -> datetime | None:
        async with self._lock:
            return await asyncio.to_thread(self.get_last_poll_watermark)

    def close(self) -> None:
        self.con.close()
