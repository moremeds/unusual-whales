from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import structlog

from uw_flow_scanner.core.schemas import FlowEvent, parse_flow_event

logger = structlog.get_logger()

# FIX 4: Maximum number of seen IDs to retain (LRU eviction, not set.clear())
_MAX_SEEN_IDS = 2000


@dataclass
class RateLimitState:
    daily_count: int = 0
    minute_remaining: int = 120

    def update_from_headers(self, headers: httpx.Headers) -> None:
        if "x-uw-daily-req-count" in headers:
            self.daily_count = int(headers["x-uw-daily-req-count"])
        if "x-uw-req-per-minute-remaining" in headers:
            self.minute_remaining = int(headers["x-uw-req-per-minute-remaining"])


class UWPoller:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        rate_limit_rpm: int = 120,
        daily_limit: int = 15000,
        retry_max: int = 3,
        retry_backoff_base: int = 2,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.rate_limit_rpm = rate_limit_rpm
        self.daily_limit = daily_limit
        self.retry_max = retry_max
        self.retry_backoff_base = retry_backoff_base
        self.rate_state = RateLimitState()
        self.watermark: datetime | None = None
        # FIX 4: Use OrderedDict as an LRU cache to evict oldest seen IDs
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=httpx.Timeout(30.0),
            )
        return self._client

    async def poll(self) -> list[FlowEvent]:
        """Poll flow-alerts endpoint. Returns new (deduplicated) events."""
        # FIX 9: Skip if rate limits are nearly exhausted
        if self.rate_state.minute_remaining < 5:
            logger.warning(
                "Rate limit nearly exhausted, skipping poll",
                minute_remaining=self.rate_state.minute_remaining,
            )
            return []
        if self.rate_state.daily_count >= self.daily_limit:
            logger.warning(
                "Daily request limit reached, skipping poll",
                daily_count=self.rate_state.daily_count,
                daily_limit=self.daily_limit,
            )
            return []

        client = await self._get_client()
        url = "/api/option-trades/flow-alerts"

        # FIX 3: Pass watermark as a query parameter so the API returns only new events.
        params: dict = {}
        if self.watermark is not None:
            params["after"] = self.watermark.isoformat()

        for attempt in range(self.retry_max):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code >= 500:
                    if attempt < self.retry_max - 1:
                        wait = self.retry_backoff_base ** attempt
                        logger.warning(
                            "UW API server error, retrying",
                            status=resp.status_code,
                            wait_secs=wait,
                            attempt=attempt + 1,
                            max_attempts=self.retry_max,
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.error(
                        "UW API server error after max retries",
                        status=resp.status_code,
                        retries=self.retry_max,
                    )
                    return []
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError:
                logger.error("UW API HTTP error", status=resp.status_code)
                return []
            except httpx.RequestError as e:
                if attempt < self.retry_max - 1:
                    wait = self.retry_backoff_base ** attempt
                    logger.warning(
                        "UW API request error, retrying", error=str(e), wait_secs=wait
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error(
                    "UW API request error after max retries",
                    error=str(e),
                    retries=self.retry_max,
                )
                return []
        else:
            return []

        self.rate_state.update_from_headers(resp.headers)

        data = resp.json().get("data", [])
        events: list[FlowEvent] = []

        for raw in data:
            uw_id = raw.get("id", "")
            if uw_id in self._seen_ids:
                continue
            # FIX 4: LRU insertion — move_to_end marks as most-recently-seen
            self._seen_ids[uw_id] = None
            self._seen_ids.move_to_end(uw_id)
            # Evict oldest entries if over capacity
            while len(self._seen_ids) > _MAX_SEEN_IDS:
                self._seen_ids.popitem(last=False)
            try:
                event = parse_flow_event(raw)
                self.update_watermark(event.tape_time)
                events.append(event)
            except Exception as e:
                logger.warning("Failed to parse flow event", uw_id=uw_id, error=str(e))

        logger.info(
            "Poll complete",
            total=len(data),
            new=len(events),
            daily_count=self.rate_state.daily_count,
            minute_remaining=self.rate_state.minute_remaining,
        )
        return events

    def update_watermark(self, tape_time: datetime) -> None:
        if self.watermark is None or tape_time > self.watermark:
            self.watermark = tape_time

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
