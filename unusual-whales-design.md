# UW Flow Scanner — Design Spec

**Date:** 2026-03-11
**Status:** Approved (revised after Codex Tribunal)
**Type:** Standalone service (separate repo: `uw-flow-scanner`)

## Goal

A near-real-time options flow scanner that polls the Unusual Whales API during market hours, scores flow events with configurable LLMs (Claude/GPT), sends actionable alerts to Discord, and accumulates a signal log for backtesting directional accuracy.

## Non-Goals (Phase 1)

- No web UI or dashboard (Discord-only delivery)
- No APEX integration (deferred to Phase 2 — adapter pattern)
- No browser scraping (pure API, $150/mo UW Basic tier)
- No WebSocket streaming (requires Advanced tier at $375/mo — polling only)
- No options P&L tracking (Phase 1 = directional accuracy only)

## Prerequisites Gate

**Before implementation begins**, obtain and verify:
1. UW API token from Basic tier subscription
2. Confirm endpoint availability: `/api/option-trades/flow-alerts`, `/api/stock/{ticker}/flow-recent`, `/api/darkpool/{ticker}`
3. Capture sample payloads for each endpoint to validate data model
4. Verify rate limit headers match research: 120 rpm, 15K/day

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│              uw-flow-scanner                               │
│                                                            │
│  ┌──────────┐    ┌────────────┐    ┌───────────┐          │
│  │ Scheduler │───▶│ UW Poller  │───▶│ Signal DB │          │
│  │ (RTH only)│    │ (httpx +   │    │ (DuckDB)  │          │
│  └──────────┘    │ watermark) │    └─────┬─────┘          │
│                   └─────┬──────┘          │                │
│                         │                 │                │
│                         ▼                 │                │
│                  ┌──────────────┐         │                │
│                  │ Aggregator   │         │                │
│                  │ (cluster +   │         │                │
│                  │  cooldown)   │         │                │
│                  └──────┬───────┘         │                │
│                         │                 │                │
│                         ▼                 │                │
│                  ┌─────────────┐          │                │
│                  │ Tier Router │          │                │
│                  └──────┬──────┘          │                │
│                    ┌────┴────┐            │                │
│                    ▼         ▼            │                │
│             ┌──────────┐ ┌──────────┐     │                │
│             │ Tier 1   │ │ Tier 2/3 │     │                │
│             │ (struct  │ │ (struct  │     │                │
│             │  output) │ │  output) │     │                │
│             └────┬─────┘ └────┬─────┘     │                │
│                  │            │            │                │
│             score > T?        ▼            │                │
│                  │     ┌───────────┐       │                │
│                  └────▶│ Discord   │       │                │
│                        │ (idemp.)  │       │                │
│                        └───────────┘       │                │
│                                            │                │
│  ┌──────────────┐   ┌─────────────┐        │                │
│  │ Health       │   │ Backtester  │◀───────┘                │
│  │ /health + HB │   │ (offline)   │                         │
│  └──────────────┘   └─────────────┘                         │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Resilience Layer                                     │    │
│  │ • UW API: 3 retries, CB after 5 failures, watermark │    │
│  │ • LLM: 2 retries, failover provider (separate cfg)  │    │
│  │ • Discord: idempotent retry with alert_key           │    │
│  └─────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

### Components

1. **Scheduler** — `asyncio` loop, active during configurable market hours (default 9:30–16:00 ET, with optional pre/post-market extension) via `pandas-market-calendars`. Polls every N seconds (configurable, default 30s — budget allows 120 rpm). Skips weekends and market holidays.

2. **UW Poller** — Calls UW API endpoints via `httpx` with rate limiting (120 rpm / 15K daily, tracked via `x-uw-*` response headers). Deduplicates by UW-provided `id` field (UUID on flow-alerts). Uses high-water-mark polling: tracks last seen event timestamp, requests overlapping windows to prevent gaps. Logs each poll cycle to `poll_log` table with completeness flags.

3. **Flow Aggregator** — Clusters related flow events before scoring. Groups events by `(ticker, 5-min window)` to detect split orders. Enforces per-ticker alert cooldown (configurable, default 10 min) to prevent spam on burst flow.

4. **Tier Router** — Routes each flow event/cluster:
   - Ticker in `watchlist.always_analyze` → Tier 3 (always full analysis)
   - All others → Tier 1 (fast score). If score ≥ threshold → Tier 2.

5. **LLM Scorer** — Multi-provider client (Anthropic + OpenAI SDKs). Model configurable per tier via YAML. Uses **structured output** (Anthropic `tool_use` / OpenAI `response_format`) for guaranteed JSON — no free-text parsing. Prompt version tracked per score for calibration. Tier 1 runs with bounded concurrency (`asyncio.Semaphore(10)`). Failover and cross-validation are separate config paths (see Configuration).

6. **Discord Alerter** — Formats Tier 2/3 results into rich embeds, sends via webhook (per-tier webhooks configurable). **Idempotent delivery:** each alert has a deterministic `alert_key = hash(flow_event_id + tier + prompt_version)`. Retry queue persists unsent alerts; suppresses duplicates on recovery.

7. **Signal DB** — DuckDB for persistence. Five tables: `flow_events`, `signal_scores`, `signal_outcomes`, `poll_log`, `scoring_jobs` (see Data Model). All timestamps stored as TIMESTAMPTZ (UTC).

8. **Backtester** — Offline tool (runs after market close). Nightly job fills `signal_outcomes` with price data. Analyzer computes directional accuracy and score calibration. Excludes time windows with failed/partial polls.

9. **Health Monitor** — Lightweight `asyncio` TCP server (no framework dependency) serving `/health` JSON. Optional Discord heartbeat at market open/close.

## UW API Reference (Verified)

**Base URL:** `https://api.unusualwhales.com`
**Auth:** `Authorization: Bearer <UW_API_KEY>`
**Docs:** `https://api.unusualwhales.com/docs` | OpenAPI: `/api/openapi`

### Key Endpoints

| Endpoint | Purpose | Filters |
|----------|---------|---------|
| `/api/option-trades/flow-alerts` | Unusual options flow | `ticker_symbol`, `min_premium`, `size_greater_oi`, `limit`, `is_otm` |
| `/api/stock/{ticker}/flow-recent` | Recent flow per ticker | — |
| `/api/darkpool/{ticker}` | Dark pool prints | — |
| `/api/stock/{ticker}/...` | OHLC, chains, Greeks, IV rank, max pain | varies |
| `/api/alerts/configuration` | Custom alert configs | — |
| `/api/alerts` | Alert records | `config_ids[]`, `limit` |

### Rate Limits (Basic Tier, $150/mo)

| Metric | Limit |
|--------|-------|
| Per-minute | 120 requests |
| Per-day | ~15,000 requests |
| Daily reset | 8:00 PM ET |
| Counting | Only successful (2xx) |

**Response headers:** `x-uw-daily-req-count`, `x-uw-token-req-limit`, `x-uw-minute-req-counter`, `x-uw-req-per-minute-remaining`, `x-uw-req-per-minute-reset`

### Data Characteristics

- Flow alerts return a `data` array with UUID `id` per record
- Dark pool prints have no unique ID (composite dedup needed)
- Historical depth via API: limited (months). Deep history: UW Data Shop (separate purchase)
- Dark pool data delayed ~15 min from execution

## Tiered Reasoning Pipeline

| Tier | Trigger | Model (default) | Latency | Output |
|------|---------|-----------------|---------|--------|
| **1: Fast scan** | Every flow cluster | Haiku | ~300ms | Score 0–100, direction, log only |
| **2: Full analysis** | Tier 1 score ≥ threshold | Sonnet | ~3–5s | Directional alert + Discord |
| **3: Watchlist** | Ticker in watchlist | Sonnet | ~5–8s | Enriched analysis + Discord |

### Tier 1: Fast Score

Input: Structured flow event data (ticker, side, premium, strike, expiry, volume, OI, underlying_price).
Output (structured): `{ "score": int, "direction": "bullish"|"bearish"|"neutral", "reasoning": str }`.
Enforced via Anthropic `tool_use` or OpenAI `response_format: { type: "json_schema" }`.
Purpose: Filter noise. ~500 events/day, most discarded.

### Tier 2: Full Analysis

Input: Flow cluster + aggregated context (recent flow for same ticker, underlying price, IV rank).
Output (structured): `{ "score": int, "direction": str, "confidence": float, "conviction_factors": [...], "reasoning": str }`.
Purpose: Actionable directional alert. **Phase 1 does not generate specific trade ideas** (strike/expiry recommendations deferred to Phase 2 when options P&L tracking is available).

### Tier 3: Watchlist Deep Dive

Input: Same as Tier 2 + additional API calls for dark pool activity, recent historical flow patterns.
Output: Same as Tier 2 + enriched context sections.
Purpose: Always-on monitoring for key tickers.

## Configuration

```yaml
# config/config.yaml

watchlist:
  always_analyze: [SPX, SPY, QQQ]

scheduler:
  market_hours_only: true
  pre_market_minutes: 0       # extend before open (0 = disabled)
  after_hours_minutes: 0      # extend after close (0 = disabled)

scoring:
  score_threshold: 75
  poll_interval_seconds: 30   # 120 rpm budget supports aggressive polling
  tier1_concurrency: 10       # max parallel Tier 1 LLM calls per batch
  tier1_timeout_seconds: 5
  tier2_timeout_seconds: 30
  alert_cooldown_seconds: 600 # per-ticker cooldown between alerts
  flow_cluster_window_seconds: 300  # group events in 5-min windows

models:
  tier1:
    provider: anthropic        # anthropic | openai
    model: claude-haiku-4-5
  tier2:
    provider: anthropic
    model: claude-sonnet-4-6
  tier3:
    provider: anthropic
    model: claude-sonnet-4-6
  failover:                    # used when primary provider is down
    provider: openai
    model: gpt-4o
  validation:                  # optional second opinion (separate from failover)
    enabled: false
    provider: openai
    model: gpt-4o

uw_api:
  base_url: https://api.unusualwhales.com
  rate_limit_rpm: 120         # UW Basic tier actual limit
  daily_limit: 15000
  # api_key via UW_API_KEY env var

discord:
  # Per-tier webhooks (fall back to default if tier-specific not set)
  # webhook_url via DISCORD_WEBHOOK_URL env var
  # tier2_webhook_url via DISCORD_TIER2_WEBHOOK_URL env var (optional)
  # tier3_webhook_url via DISCORD_TIER3_WEBHOOK_URL env var (optional)

resilience:
  uw_retries: 3
  uw_circuit_breaker_threshold: 5   # consecutive failures before pause
  uw_circuit_breaker_pause_seconds: 300
  llm_retries: 2
  discord_retry_max: 3
  discord_retry_window_minutes: 60

health:
  enabled: true
  port: 8090
  heartbeat_discord: true     # send open/close heartbeat to Discord

storage:
  db_path: data/signals.duckdb

backtest:
  win_threshold_pct: 1.0      # underlying move >= 1% in predicted direction = win
  scratch_band_pct: 0.5       # moves within +/-0.5% = scratch
  evaluation_window_days: 5   # primary evaluation horizon

logging:
  level: INFO
  format: json
  file: logs/scanner.log
  rotation: 10MB

ops:
  process_supervisor: systemd  # or: launchd, docker, manual
  retention_days: 90           # DuckDB data retention
  backup_enabled: false        # Phase 1: manual backups
  daily_spend_cap_usd: 10.0   # hard cap on LLM spend per day
```

**Secrets** via environment variables only: `UW_API_KEY`, `DISCORD_WEBHOOK_URL`, `DISCORD_TIER2_WEBHOOK_URL`, `DISCORD_TIER3_WEBHOOK_URL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

## Data Model

All timestamps are `TIMESTAMPTZ` (stored as UTC). Market session dates derived from UTC + ET conversion.

### flow_events
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Internal primary key |
| uw_event_id | VARCHAR | UW-provided UUID (primary dedup key for flow-alerts) |
| ingested_at | TIMESTAMPTZ | When we received this event |
| tape_time | TIMESTAMPTZ | When UW recorded the flow (source timestamp) |
| ticker | VARCHAR | Underlying symbol |
| underlying_price | DECIMAL | Underlying price at poll time |
| flow_type | VARCHAR | sweep, block, split, etc. |
| side | VARCHAR | call / put |
| sentiment | VARCHAR | bullish / bearish / neutral |
| premium | DECIMAL | Total premium |
| strike | DECIMAL | Strike price |
| expiry | DATE | Option expiration |
| volume | INTEGER | Contract volume |
| open_interest | INTEGER | Open interest |
| raw_json | JSON | Full UW API response for replay |

**Indexes:** `(ticker, tape_time)`, `UNIQUE (uw_event_id)`

### scoring_jobs
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| flow_event_id | UUID | FK to flow_events |
| status | VARCHAR | pending / in_progress / completed / failed / expired |
| tier | INTEGER | Target tier (1, 2, or 3) |
| retry_count | INTEGER | Number of attempts |
| created_at | TIMESTAMPTZ | Job creation time |
| updated_at | TIMESTAMPTZ | Last status change |
| error | TEXT | Error message if failed |

**Indexes:** `(status, created_at)` for recovery queue

### signal_scores
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| flow_event_id | UUID | FK to flow_events |
| tier | INTEGER | 1, 2, or 3 |
| model_used | VARCHAR | e.g., claude-sonnet-4-6 |
| prompt_version | VARCHAR | e.g., v1.0.0 — for calibration tracking |
| score | INTEGER | 0–100 |
| direction | VARCHAR | bullish / bearish / neutral |
| confidence | DECIMAL | 0.0–1.0 |
| reasoning | TEXT | LLM rationale |
| raw_output | JSON | Full structured LLM response |
| alert_key | VARCHAR | Deterministic `hash(flow_event_id + tier + prompt_version)` |
| alert_status | VARCHAR | pending / sent / failed / skipped |
| alert_attempts | INTEGER | Send attempt count |
| created_at | TIMESTAMPTZ | Score timestamp |

**Indexes:** `(tier, score)`, `(flow_event_id)`, `(alert_status, created_at)`, `UNIQUE (alert_key)`

### signal_outcomes
| Column | Type | Description |
|--------|------|-------------|
| signal_id | UUID | FK to signal_scores |
| ticker | VARCHAR | Symbol |
| entry_price | DECIMAL | Underlying price at signal time |
| price_1d | DECIMAL | Price after 1 trading day |
| price_3d | DECIMAL | Price after 3 trading days |
| price_5d | DECIMAL | Price after 5 trading days |
| price_10d | DECIMAL | Price after 10 trading days |
| max_favorable | DECIMAL | Best price in evaluation window |
| max_adverse | DECIMAL | Worst price in evaluation window |
| outcome | VARCHAR | win / loss / scratch (per backtest config thresholds) |
| updated_at | TIMESTAMPTZ | Last fill time |

**Indexes:** `UNIQUE (signal_id)`

**Phase 1 limitation:** Tracks underlying price movement for directional accuracy only. True options P&L (option greeks, IV at entry, strategy payoff) requires Phase 2.

### poll_log
| Column | Type | Description |
|--------|------|-------------|
| poll_id | UUID | Primary key |
| started_at | TIMESTAMPTZ | Poll cycle start |
| completed_at | TIMESTAMPTZ | Poll cycle end |
| status | VARCHAR | success / partial / failed |
| events_fetched | INTEGER | Total events from API |
| events_new | INTEGER | After deduplication |
| high_water_mark | VARCHAR | Last event ID for cursor-based polling |
| uw_daily_count | INTEGER | From `x-uw-daily-req-count` header |
| uw_minute_remaining | INTEGER | From `x-uw-req-per-minute-remaining` header |
| error | TEXT | Error message if any |

**Indexes:** `(started_at)`, `(status)`

## Project Structure

```
uw-flow-scanner/
├── config/
│   └── config.yaml
├── src/
│   ├── main.py              # entry point, scheduler loop
│   ├── config.py            # pydantic settings loader
│   ├── health.py            # /health endpoint (plain asyncio TCP)
│   ├── poller/
│   │   ├── uw_client.py     # UW API client (httpx async + rate limit + watermark)
│   │   ├── dedup.py         # flow event deduplication
│   │   └── aggregator.py    # flow clustering + alert cooldown
│   ├── scoring/
│   │   ├── router.py        # tier routing logic
│   │   ├── prompts.py       # versioned prompt templates per tier
│   │   ├── schemas.py       # pydantic models for structured LLM output
│   │   ├── llm_client.py    # multi-provider LLM wrapper (structured output)
│   │   └── heuristic.py     # rule-based fallback scorer (zero-LLM)
│   ├── alerting/
│   │   └── discord.py       # webhook formatter + idempotent sender
│   ├── resilience/
│   │   └── retry.py         # tenacity policies, circuit breaker, failover
│   ├── storage/
│   │   └── db.py            # DuckDB init, insert, query, indexes, retention
│   └── backtest/
│       ├── outcome_filler.py # nightly: fill price outcomes
│       └── analyzer.py       # directional accuracy + score calibration
├── tests/
│   ├── test_dedup.py
│   ├── test_aggregator.py
│   ├── test_router.py
│   ├── test_scoring.py
│   ├── test_schemas.py
│   └── test_storage.py
├── pyproject.toml
└── README.md
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `httpx` | Async HTTP for UW API |
| `anthropic` | Claude SDK (structured output via tool_use) |
| `openai` | GPT SDK (structured output via response_format) |
| `duckdb` | Signal storage + analytics |
| `pydantic` | Config validation + LLM output schemas |
| `pandas-market-calendars` | RTH schedule, holidays |
| `tenacity` | Retry + circuit breaker |
| `structlog` | Structured JSON logging with secret redaction |

## Cost Estimate (Monthly, Token-Budget Model)

### Assumptions
- ~500 flow events/day × 21 trading days = ~10,500 events/month
- Tier 1 prompt: ~200 input tokens, ~50 output tokens per event
- Tier 2 (6% hit rate, ~30/day): ~800 input tokens, ~200 output tokens
- Tier 3 (watchlist, ~20/day): ~1,200 input tokens, ~300 output tokens
- Retry overhead: +15%

### Token Costs (current Anthropic/OpenAI pricing)

| Tier | Events/mo | Input tokens | Output tokens | Cost/mo |
|------|-----------|-------------|---------------|---------|
| Tier 1 (Haiku) | 10,500 | 2.1M | 0.5M | ~$3 |
| Tier 2 (Sonnet) | 630 | 0.5M | 0.13M | ~$4 |
| Tier 3 (Sonnet) | 420 | 0.5M | 0.13M | ~$4 |
| Retry overhead (+15%) | — | — | — | ~$2 |
| **LLM subtotal** | | | | **~$13/mo** |

| Item | Cost |
|------|------|
| UW API Basic tier | $150/mo |
| LLM (all tiers) | ~$13/mo |
| Cross-validation (GPT-4o, optional) | ~$5/mo |
| **Total** | **~$163–168/mo** |

**Daily spend cap:** $10/day hard limit in config (prevents runaway costs from retry loops).

## Resilience Strategy

### Provider Failover (separate from validation)

```yaml
# failover: used when primary is DOWN
failover:
  provider: openai
  model: gpt-4o

# validation: optional second opinion (independent)
validation:
  enabled: false
  provider: openai
  model: gpt-4o
```

Failover activates on 3 consecutive failures from primary. Validation runs in parallel when enabled and both providers are healthy. They are separate config paths with separate budgets.

### Failure Modes
| Failure | Response |
|---------|----------|
| UW API 5xx | Retry 3x exponential backoff. After 5 consecutive failures, circuit breaker pauses 5 min |
| UW API partial response | Log to `poll_log` as `partial`, retry once, proceed with available data |
| LLM provider down | Retry 2x, failover to secondary provider, then heuristic (Tier 1) or queue to `scoring_jobs` (Tier 2/3) |
| Discord webhook failure | Idempotent retry using `alert_key`. Max 3 attempts within 1 hour |
| DuckDB write failure | Log error, buffer in memory, retry next cycle |

### Watermark-Based Polling
Each poll remembers the last seen `uw_event_id` and `tape_time`. Next poll requests an overlapping time window (current - 5 min) to catch events that appeared late. Dedup by `uw_event_id` handles the overlap safely.

### Scoring Job Recovery
Events that fail LLM scoring create a `scoring_jobs` record with `status = failed`. Recovery loop (every 5 min) retries jobs < 1 hour old. Jobs > 1 hour old expire.

## Operations

### Deployment
- **Phase 1:** Single process, `systemd` unit file (Linux) or `launchd` plist (macOS)
- Graceful shutdown: `SIGTERM` handler drains current poll cycle, flushes pending writes
- Startup recovery: on start, scan `scoring_jobs` for `in_progress` (stale), reset to `pending`

### Retention & Backups
- DuckDB data retained for 90 days (configurable), nightly `DELETE WHERE tape_time < now() - interval '90 days'`
- Manual DuckDB file backup (Phase 1). Automated backup deferred.

### Monitoring
- `/health` returns `{ "status": "ok|degraded|unhealthy", "last_poll": "...", "uw_daily_remaining": N, "llm_provider": "..." }`
- Discord heartbeat at market open ("Scanner starting") and close ("Scanner stopping, N events scored today")
- Structured logs (JSON) with automatic secret redaction for `UW_API_KEY`, webhook URLs

## Future: APEX Integration (Phase 2)

When ready to wire into APEX:
- APEX adapter (`src/infrastructure/adapters/uw_flow/`) reads from `uw-flow-scanner`'s DuckDB or REST endpoint
- UW signals become inputs to APEX risk engine (portfolio exposure check before acting)
- Add options P&L tracking: option greeks at entry, IV context, theoretical strategy payoff
- Upgrade Tier 2/3 output to include specific trade ideas (strike/expiry/strategy)
- Consider UW Advanced tier ($375/mo) for WebSocket streaming if latency matters

No APEX code changes needed in Phase 1.

## Success Criteria

1. Scanner runs unattended during market hours, polling every 30s
2. Tier 1 scores every flow event within 1s (batch of 50 within 15s with concurrency)
3. High-conviction signals (Tier 2/3) reach Discord within 15s of ingestion
4. All signals + scores + reasoning logged to DuckDB with prompt version
5. After 2 weeks of accumulation, backtest analyzer produces directional accuracy and score calibration stats
6. Model per tier is swappable via config change (no code edit)
7. Service recovers gracefully from any single external dependency outage
8. `/health` endpoint reports service status and last successful poll timestamp
9. No duplicate Discord alerts (idempotent delivery verified)
10. Daily LLM spend stays within configured cap

## Tribunal Review Log

**Reviewers:** Codex gpt-5.4 (weight 1.0) + Claude (weight 1.0) + Internet Research
**Gemini:** Unavailable (auth blocked) — bilateral mode

### Issues Addressed In This Revision

| # | Issue | Source | Fix Applied |
|---|-------|--------|-------------|
| 1 | Cost estimate math wrong + needs token-budget model | Codex | Rebuilt with per-tier token budget, verified pricing |
| 2 | Polling can miss events without cursor/watermark | Codex | Added watermark-based polling with overlap |
| 3 | UW API assumptions unverified | Codex + Research | Added verified API reference section with endpoints, rate limits, auth |
| 4 | `scored` column missing from schema | Codex + Claude | Replaced with explicit `scoring_jobs` table |
| 5 | LLM output needs structured schema | Codex + Claude | Mandated structured output (tool_use/response_format), added schemas.py |
| 6 | Failover and cross-validation improperly coupled | Codex | Split into separate `failover` and `validation` config |
| 7 | No flow aggregation / alert spam on burst | Claude | Added Aggregator component with clustering + cooldown |
| 8 | Discord alerting not idempotent | Codex | Added `alert_key` with deterministic hash, dedup on retry |
| 9 | Timestamps need UTC/timezone awareness | Codex | All timestamps → TIMESTAMPTZ, UTC storage documented |
| 10 | Ops section missing (deployment, retention, restart) | Codex | Added Operations section |
| 11 | Backtest outcome model doesn't match product output | Codex | Narrowed Phase 1 to directional alerts only, trade ideas deferred |
| 12 | Rate limits wrong (60 → 120 rpm) | Research | Updated to verified 120 rpm, 15K/day |
| 13 | "Real-time" label misleading | Codex | Changed to "near-real-time" throughout |
| 14 | Security beyond env vars | Codex | Added structured log redaction, daily spend cap |
| 15 | `prompt_version` not tracked | Claude + Codex | Added to signal_scores for calibration |
