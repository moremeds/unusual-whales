from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()


class HealthState:
    def __init__(self):
        self.status: str = "starting"
        self.last_poll: datetime | None = None
        self.events_polled: int = 0
        self.uw_daily_remaining: int | None = None
        self.llm_provider: str = "anthropic"

    def record_poll(self, events_polled: int, uw_daily_remaining: int) -> None:
        self.status = "ok"
        self.last_poll = datetime.now(timezone.utc)
        self.events_polled = events_polled
        self.uw_daily_remaining = uw_daily_remaining

    def mark_degraded(self, reason: str) -> None:
        self.status = "degraded"
        logger.warning("Health degraded", reason=reason)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "last_poll": self.last_poll.isoformat() if self.last_poll else None,
            "events_polled": self.events_polled,
            "uw_daily_remaining": self.uw_daily_remaining,
            "llm_provider": self.llm_provider,
        }


class HealthServer:
    def __init__(self, state: HealthState, port: int = 8090):
        self.state = state
        self.port = port
        self._server: asyncio.Server | None = None

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            except (TimeoutError, asyncio.TimeoutError):
                return

            # Parse request line and only respond to GET /health
            request_line = data.decode(errors="replace").split("\r\n", 1)[0]
            parts = request_line.split()
            if len(parts) >= 2 and parts[0] == "GET" and parts[1] == "/health":
                body = json.dumps(self.state.to_dict())
                response = (
                    f"HTTP/1.0 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )
            else:
                body = '{"error": "not found"}'
                response = (
                    f"HTTP/1.0 404 Not Found\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )
            writer.write(response.encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_request, "127.0.0.1", self.port
        )
        # If port was 0, get the actual assigned port
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]
        logger.info("Health server listening", port=self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
