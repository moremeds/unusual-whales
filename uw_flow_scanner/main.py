from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timezone

import structlog
from anthropic import AsyncAnthropic

from uw_flow_scanner.alerting.discord import DiscordAlerter
from uw_flow_scanner.core.config import AppConfig, load_config
from uw_flow_scanner.core.db import SignalDB
from uw_flow_scanner.core.schemas import FlowEvent
from uw_flow_scanner.health.server import HealthServer, HealthState
from uw_flow_scanner.ingestion.poller import UWPoller
from uw_flow_scanner.scoring.scorer import LLMScorer, SpendTracker

logger = structlog.get_logger()


def _setup_logging(cfg: AppConfig) -> None:
    """Configure structlog."""
    renderer = (
        structlog.dev.ConsoleRenderer()
        if cfg.logging.format != "json"
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        logger_factory=structlog.PrintLoggerFactory(),
    )


def _is_market_open() -> bool:
    """Check if US equity market is currently open using pandas-market-calendars."""
    try:
        import pandas_market_calendars as mcal

        nyse = mcal.get_calendar("NYSE")
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        schedule = nyse.schedule(start_date=today, end_date=today)
        if schedule.empty:
            return False
        market_open = schedule.iloc[0]["market_open"].to_pydatetime()
        market_close = schedule.iloc[0]["market_close"].to_pydatetime()
        return market_open <= now <= market_close
    except Exception as e:
        logger.warning("Market calendar check failed, assuming open", error=str(e))
        return True


class Scanner:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._stop_event = asyncio.Event()
        self.health_state = HealthState()

        # Components
        self.db = SignalDB(cfg.storage.db_path)
        self.db.init_tables()

        self.poller = UWPoller(
            base_url=cfg.uw_api.base_url,
            api_key=cfg.uw_api_key,
            rate_limit_rpm=cfg.uw_api.rate_limit_rpm,
            daily_limit=cfg.uw_api.daily_limit,
            retry_max=cfg.uw_api.retry_max,
            retry_backoff_base=cfg.uw_api.retry_backoff_base,
        )

        self.spend_tracker = SpendTracker(
            daily_cap_usd=cfg.ops.daily_spend_cap_usd,
            token_rates={
                k: {"input_per_mtok": v.input_per_mtok, "output_per_mtok": v.output_per_mtok}
                for k, v in cfg.ops.token_rates.items()
            },
        )

        anthropic_client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
        self.scorer = LLMScorer(
            client=anthropic_client,
            tier1_model=cfg.models.tier1.model,
            tier2_model=cfg.models.tier2.model,
            tier1_timeout=cfg.scoring.tier1_timeout_seconds,
            tier2_timeout=cfg.scoring.tier2_timeout_seconds,
            spend_tracker=self.spend_tracker,
        )

        self.alerter = DiscordAlerter(webhook_url=cfg.discord_webhook_url)
        self.health_server = HealthServer(self.health_state, port=cfg.health.port)

        # Cooldown state: ticker -> last alert datetime
        self._cooldowns: dict[str, datetime] = {}

    async def _hydrate_state(self) -> None:
        """Restore watermark and cooldowns from DB on startup."""
        watermark = await self.db.async_get_last_poll_watermark()
        if watermark is not None:
            self.poller.watermark = watermark
            logger.info("Restored watermark from DB", watermark=watermark.isoformat())

        # Restore cooldowns for tickers that have recent sent alerts
        rows = self.db.con.execute(
            """
            SELECT fe.ticker, MAX(ss.created_at) as last_alert
            FROM signal_scores ss
            JOIN flow_events fe ON fe.id = ss.flow_event_id
            WHERE ss.alert_status = 'sent'
            GROUP BY fe.ticker
            """
        ).fetchall()
        for ticker, last_alert in rows:
            ts = self.db._ensure_utc(last_alert)
            elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
            if elapsed < self.cfg.scoring.alert_cooldown_seconds:
                self._cooldowns[ticker] = ts
        if self._cooldowns:
            logger.info("Restored cooldowns from DB", tickers=list(self._cooldowns.keys()))

    @property
    def running(self) -> bool:
        return not self._stop_event.is_set()

    @running.setter
    def running(self, value: bool) -> None:
        if not value:
            self._stop_event.set()

    def _is_cooled_down(self, ticker: str) -> bool:
        """Check if ticker is in cooldown period."""
        last_alert = self._cooldowns.get(ticker)
        if last_alert is None:
            return True
        elapsed = (datetime.now(timezone.utc) - last_alert).total_seconds()
        return elapsed >= self.cfg.scoring.alert_cooldown_seconds

    async def _process_event(self, event: FlowEvent, semaphore: asyncio.Semaphore) -> None:
        """Score a single event through the tier pipeline."""
        async with semaphore:
            is_new = await self.db.async_insert_flow_event(event)
            if not is_new:
                logger.debug("Duplicate event skipped", uw_event_id=event.uw_event_id)
                return

            # Check cooldown
            if not self._is_cooled_down(event.ticker):
                return

            # Tier 1: Fast scan
            t1_result = await self.scorer.score_tier1(event)
            if t1_result is None:
                return

            # Log Tier 1 score
            await self.db.async_insert_signal_score(
                uw_event_id=event.uw_event_id,
                flow_event_id=event.id,
                tier=1,
                model_used=self.cfg.models.tier1.model,
                prompt_version=self.scorer.prompt_version,
                score=t1_result.score,
                direction=t1_result.direction,
                confidence=None,
                reasoning=t1_result.reasoning,
                raw_output=t1_result.model_dump(),
            )

            # Tier 2 if score meets threshold
            if t1_result.score < self.cfg.scoring.score_threshold:
                return

            t2_result = await self.scorer.score_tier2(event)
            if t2_result is None:
                return

            score_id = await self.db.async_insert_signal_score(
                uw_event_id=event.uw_event_id,
                flow_event_id=event.id,
                tier=2,
                model_used=self.cfg.models.tier2.model,
                prompt_version=self.scorer.prompt_version,
                score=t2_result.score,
                direction=t2_result.direction,
                confidence=t2_result.confidence,
                reasoning=t2_result.reasoning,
                raw_output=t2_result.model_dump(),
            )

            # Skip alert if this was a duplicate score (phantom UUID)
            if score_id is None:
                logger.debug("Duplicate score skipped", uw_event_id=event.uw_event_id)
                return

            # Send Discord alert
            success = await self.alerter.send_alert(event, t2_result)
            status = "sent" if success else "failed"
            await self.db.async_update_alert_status(score_id, status)

            if success:
                self._cooldowns[event.ticker] = datetime.now(timezone.utc)

    async def run_cycle(self) -> None:
        """Execute one poll -> score -> alert cycle."""
        # Check budget exhaustion (resets daily via SpendTracker._check_reset)
        if self.spend_tracker.is_budget_exhausted and not self.spend_tracker.budget_alert_sent:
            msg = (
                f"[UW Flow Scanner] BUDGET EXHAUSTED: daily spend "
                f"${self.spend_tracker.daily_spend_usd:.4f} >= "
                f"cap ${self.spend_tracker.daily_cap_usd:.2f}. "
                f"Tier 2 scoring disabled until UTC midnight."
            )
            await self.alerter.send_text(msg)
            self.spend_tracker.budget_alert_sent = True
            logger.warning(
                "Budget exhausted alert sent",
                spend=self.spend_tracker.daily_spend_usd,
                cap=self.spend_tracker.daily_cap_usd,
            )

        events = await self.poller.poll()

        self.health_state.record_poll(
            events_polled=len(events),
            uw_daily_remaining=self.cfg.uw_api.daily_limit - self.poller.rate_state.daily_count,
        )

        if not events:
            return

        semaphore = asyncio.Semaphore(self.cfg.scoring.tier1_concurrency)
        tasks = [self._process_event(event, semaphore) for event in events]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log any exceptions from individual event processing
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "Event processing failed",
                    event_index=i,
                    error=str(result),
                    exc_info=result,
                )

        logger.info(
            "Cycle complete",
            events=len(events),
            spend=f"${self.spend_tracker.daily_spend_usd:.4f}",
        )

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep for `seconds` but wake immediately on stop signal. Returns True if stopped."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True  # stop was signaled
        except (TimeoutError, asyncio.TimeoutError):
            return False  # timeout expired normally

    async def run(self) -> None:
        """Main loop: poll at configured interval during market hours."""
        # Hydrate state from DB before first poll
        await self._hydrate_state()

        if self.cfg.health.enabled:
            await self.health_server.start()

        logger.info("Scanner started", poll_interval=self.cfg.scheduler.poll_interval_seconds)

        try:
            while self.running:
                if self.cfg.scheduler.market_hours_only and not _is_market_open():
                    logger.debug("Market closed, sleeping 60s")
                    if await self._sleep_or_stop(60):
                        break
                    continue

                try:
                    await self.run_cycle()
                except Exception as e:
                    logger.error("Cycle error", error=str(e), exc_info=True)
                    self.health_state.mark_degraded(str(e))

                if await self._sleep_or_stop(self.cfg.scheduler.poll_interval_seconds):
                    break
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down scanner...")
        await self.poller.close()
        await self.alerter.close()
        if self.cfg.health.enabled:
            await self.health_server.stop()
        self.db.close()
        logger.info("Scanner stopped")


def cli() -> None:
    """CLI entry point."""
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/config.yaml"

    try:
        cfg = load_config(config_path)
    except (ValueError, FileNotFoundError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    _setup_logging(cfg)

    scanner = Scanner(cfg)

    def handle_signal(sig: int, frame) -> None:
        scanner.running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(scanner.run())


if __name__ == "__main__":
    cli()
