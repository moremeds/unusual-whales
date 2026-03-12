# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

UW Flow Scanner — a near-real-time options flow scanner that polls the Unusual Whales API during market hours, scores flow events with LLMs (Claude/GPT), sends actionable alerts to Discord via webhook, and logs signals to DuckDB for backtesting directional accuracy.

**Status:** Pre-implementation. Design specs in `docs/superpowers/specs/` and `unusual-whales-design.md`.

## Architecture

The system follows a phased rollout (1a → 1b → 1c → 2a → 2b). Phase 1a (MVP) is the implementation target:

```
Scheduler (RTH only, 30s poll)
  → UW Poller (httpx, watermark dedup, /api/option-trades/flow-alerts)
    → Per-ticker Cooldown (10 min)
      → LLM Scorer (Tier 1: Haiku fast scan → Tier 2: Sonnet full analysis if score ≥ 75)
        → Discord Alerter (webhook, idempotent via alert_key hash)
        → Signal DB (DuckDB: flow_events + signal_scores tables)
  Health endpoint (/health on port 8090)
```

All LLM output uses **structured output** (Anthropic `tool_use`) — no free-text parsing. Prompt versions are tracked per score for calibration.

## Tech Stack

- **Python 3.12+**, async throughout (`asyncio`)
- **httpx** — async HTTP for UW API
- **anthropic** — Claude SDK with structured output
- **duckdb** — signal storage and analytics
- **pydantic** — config validation and LLM output schemas
- **pandas-market-calendars** — market hours, holidays
- **structlog** — JSON logging with secret redaction

## Planned Project Structure (MVP)

```
uw-flow-scanner/
├── config/config.yaml
├── src/
│   ├── main.py          # entry point, scheduler loop
│   ├── config.py         # pydantic settings loader
│   ├── health.py         # /health endpoint (plain asyncio TCP)
│   ├── poller.py         # UW API client + watermark + dedup
│   ├── scorer.py         # Tier 1/2 LLM scoring
│   ├── prompts.py        # versioned prompt templates
│   ├── schemas.py        # pydantic models for LLM output
│   ├── discord.py        # webhook formatter + sender
│   └── db.py             # DuckDB init, insert, query
├── tests/
├── pyproject.toml
```

Flat `src/` in MVP — subdirectories (`enrichment/`, `resilience/`) introduced in Phase 1b/1c.

## Key Design Decisions

- **Watermark polling:** Each poll tracks last `tape_time` and requests overlapping 5-min windows to prevent gaps. Dedup by `uw_event_id`.
- **Two-tier scoring:** Tier 1 (Haiku, ~300ms) filters noise. Only events scoring ≥ 75 get Tier 2 (Sonnet, ~3-5s) for Discord alerts.
- **Idempotent alerts:** `alert_key = hash(flow_event_id + tier + prompt_version)` prevents duplicate Discord messages.
- **No retry queue in MVP:** Failed LLM calls are logged and skipped. Retry queue added in Phase 1b.
- **DuckDB over SQLite/Postgres:** Columnar storage optimized for analytics queries in backtesting.

## UW API Constraints

- Basic tier ($150/mo): 120 RPM, 15K requests/day, reset 8:00 PM ET
- Only 2xx responses count against limits
- Flow alerts have UUID `id` for dedup; dark pool prints do not
- Rate limit state available via `x-uw-*` response headers

## Secrets (env vars only)

`UW_API_KEY`, `ANTHROPIC_API_KEY`, `DISCORD_WEBHOOK_URL`. Never commit these. The `.env` file is gitignored.

## Design Specs

- `docs/superpowers/specs/2026-03-12-uw-flow-scanner-phased-design.md` — phased design (authoritative)
- `unusual-whales-design.md` — original monolithic spec (superseded, kept for reference)
