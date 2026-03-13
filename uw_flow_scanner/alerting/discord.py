from __future__ import annotations

from datetime import datetime, timezone

import httpx
import structlog

from uw_flow_scanner.core.schemas import FlowEvent, Tier2Result

logger = structlog.get_logger()

# Embed colors by direction
COLORS = {
    "bullish": 0x00FF00,   # green
    "bearish": 0xFF0000,   # red
    "neutral": 0xFFFF00,   # yellow
}


def _truncate(text: str, limit: int) -> str:
    """Truncate text to fit Discord's field limits, adding ellipsis if cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_embed(event: FlowEvent, result: Tier2Result) -> dict:
    """Build a Discord rich embed from a scored flow event."""
    direction = result.direction
    color = COLORS.get(direction, 0x808080)

    # Discord limits: title=256, field.name=256, field.value=1024
    conviction_text = "\n".join(f"• {f}" for f in result.conviction_factors[:10]) or "N/A"

    return {
        "title": _truncate(
            f"{'🟢' if direction == 'bullish' else '🔴' if direction == 'bearish' else '🟡'}"
            f" {event.ticker} — {direction.upper()} ({result.score}/100)",
            256,
        ),
        "color": color,
        "fields": [
            {"name": "Score", "value": str(result.score), "inline": True},
            {"name": "Direction", "value": direction.upper(), "inline": True},
            {"name": "Confidence", "value": f"{result.confidence:.0%}", "inline": True},
            {"name": "Flow Type", "value": event.flow_type.upper(), "inline": True},
            {"name": "Side", "value": event.side.upper(), "inline": True},
            {"name": "Premium", "value": f"${float(event.premium):,.0f}", "inline": True},
            {"name": "Strike", "value": f"${event.strike}", "inline": True},
            {"name": "Expiry", "value": str(event.expiry), "inline": True},
            {"name": "Underlying", "value": f"${event.underlying_price}", "inline": True},
            {
                "name": "Vol / OI",
                "value": f"{event.volume:,} / {event.open_interest:,}",
                "inline": True,
            },
            {
                "name": "Conviction Factors",
                "value": _truncate(conviction_text, 1024),
                "inline": False,
            },
            {
                "name": "Reasoning",
                "value": _truncate(result.reasoning, 1024),
                "inline": False,
            },
        ],
        "footer": {
            "text": f"UW Flow Scanner | {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
        },
    }


class DiscordAlerter:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def send_alert(self, event: FlowEvent, result: Tier2Result) -> bool:
        """Send a Discord alert. Returns True on success, False on failure."""
        embed = build_embed(event, result)
        payload = {"embeds": [embed]}

        try:
            client = await self._get_client()
            resp = await client.post(self.webhook_url, json=payload)
            if resp.status_code in (200, 204):
                logger.info("Discord alert sent", ticker=event.ticker, score=result.score)
                return True
            else:
                logger.error(
                    "Discord webhook error",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                return False
        except Exception as e:
            logger.error("Discord send failed", ticker=event.ticker, error=str(e))
            return False

    async def send_text(self, message: str) -> bool:
        """Send a plain text message to Discord. Used for ops alerts."""
        try:
            client = await self._get_client()
            resp = await client.post(self.webhook_url, json={"content": message})
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.error("Discord text send failed", error=str(e))
            return False

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
