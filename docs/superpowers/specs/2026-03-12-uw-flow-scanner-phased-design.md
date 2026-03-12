# UW Flow Scanner — Phased Design Spec

**Date:** 2026-03-12
**Status:** Draft
**Type:** Standalone service (separate repo: `uw-flow-scanner`)
**Supersedes:** `unusual-whales-design.md` (monolithic spec)

## Goal

A near-real-time options flow scanner that polls the Unusual Whales API during market hours, scores flow events with configurable LLMs, sends actionable alerts to Discord, and accumulates a signal log for backtesting directional accuracy.

**Principle:** Ship the MVP, prove it prints, then enrich.

## Phase Map

```
Phase 1a (MVP)        Flow → Score → Discord → Log
                      1 endpoint, 2 tables, single LLM provider, webhook

Phase 1b (Harden)     + retry queue, failover, circuit breaker,
                        clustering, health heartbeat, structured logs,
                        minimal backtester (signal_outcomes + nightly fill)

Phase 1c (Enrich)     + earnings, market tide, IV rank, calendars,
                        max pain, dark pool, Tier 3 watchlist

Phase 2a (Alpha)      + congress trading, insiders, GEX, seasonality,
                        full backtester analytics, prompt versioning,
                        Discord bot

Phase 2b (Scale)      + WebSocket ($375/mo), MCP server, APEX adapter,
                        options P&L tracking
```

---

# Phase 1a: MVP — "Flow In, Alerts Out"

## Goal

Minimum viable loop: poll flow alerts → score with LLM → post to Discord → log to DuckDB. Prove the system produces useful directional signals before investing in resilience or enrichment.

## Non-Goals (Phase 1a)

- No flow clustering or aggregation (score each event individually)
- No Tier 3 / watchlist deep dives
- No LLM failover or heuristic fallback
- No dark pool data
- No backtesting
- No Discord bot (webhook only)
- No retry queue for failed LLM calls
- No pre/post-market extension
- No WebSocket streaming

## Prerequisites Gate

**Before implementation begins**, obtain and verify:
1. UW API token from Basic tier subscription ($150/mo)
2. Confirm endpoint: `GET /api/option-trades/flow-alerts` returns data with UUID `id` field
3. Capture a sample payload to validate the data model below
4. Verify rate limit headers: `x-uw-daily-req-count`, `x-uw-req-per-minute-remaining`
5. Create Discord server + webhook URL for alerts channel

## Architecture

```
┌─────────────────────────────────────────────┐
│              uw-flow-scanner (MVP)           │
│                                             │
│  ┌──────────┐    ┌────────────┐             │
│  │ Scheduler │───▶│ UW Poller  │             │
│  │ (RTH only)│    │ (httpx +   │             │
│  └──────────┘    │ watermark) │             │
│                   └─────┬──────┘             │
│                         │                    │
│                         ▼                    │
│                  ┌──────────────┐            │
│                  │ Cooldown     │            │
│                  │ (per-ticker) │            │
│                  └──────┬───────┘            │
│                         │                    │
│                         ▼                    │
│                  ┌─────────────┐             │
│                  │ LLM Scorer  │             │
│                  │ (Tier 1→2)  │             │
│                  └──────┬──────┘             │
│                         │                    │
│                    score ≥ T?                │
│                         │ yes                │
│                         ▼                    │
│                  ┌───────────┐  ┌─────────┐ │
│                  │ Discord   │  │Signal DB│ │
│                  │ (webhook) │  │(DuckDB) │ │
│                  └───────────┘  └─────────┘ │
│                                             │
│  ┌──────────────┐                           │
│  │ Health /health│                           │
│  └──────────────┘                           │
└─────────────────────────────────────────────┘
```

### Components

1. **Scheduler** — `asyncio` loop, active during market hours (9:30–16:00 ET) via `pandas-market-calendars`. Polls every 30 seconds. Skips weekends and holidays.

2. **UW Poller** — Calls `GET /api/option-trades/flow-alerts` via `httpx` with rate limiting (120 rpm / 15K daily). Deduplicates by UW-provided `id` field (UUID). Uses watermark polling: tracks last seen `tape_time`, requests overlapping 5-min windows. Simple exponential backoff on failure (3 retries), then skip cycle.

3. **Cooldown** — Per-ticker alert cooldown (default 10 min). If ticker was alerted recently, suppress. No clustering in MVP — each event scored individually.

4. **LLM Scorer** — Anthropic SDK only. Haiku for Tier 1 (fast scan), Sonnet for Tier 2 (full analysis). Uses structured output via `tool_use` for guaranteed JSON. Bounded concurrency (`asyncio.Semaphore(10)`) for Tier 1 batches. On failure: log error, skip event (no retry queue in MVP).

5. **Discord Alerter** — Single webhook URL. Formats Tier 2 results into rich embeds. Idempotent delivery via `alert_key = hash(flow_event_id + tier + prompt_version)`. On failure: log, move on.

6. **Signal DB** — DuckDB with 2 tables: `flow_events`, `signal_scores`. All timestamps TIMESTAMPTZ (UTC).

7. **Health** — Lightweight `asyncio` TCP server serving `/health` JSON. No Discord heartbeat in MVP.

## UW API Reference

**Base URL:** `https://api.unusualwhales.com`
**Auth:** `Authorization: Bearer <UW_API_KEY>`

### MVP Endpoint

| Endpoint | Purpose | Key Fields |
|----------|---------|------------|
| `GET /api/option-trades/flow-alerts` | Unusual options flow | `id` (UUID), `ticker_symbol`, `sentiment`, `total_premium`, `strike_price`, `expires`, `volume`, `open_interest`, `underlying_price`, `option_type`, `flow_type` |

**Filters:** `ticker_symbol`, `min_premium`, `size_greater_oi`, `limit`, `is_otm`

### Rate Limits (Basic Tier, $150/mo)

| Metric | Limit |
|--------|-------|
| Per-minute | 120 requests |
| Per-day | ~15,000 requests |
| Daily reset | 8:00 PM ET |
| Counting | Only successful (2xx) |

**Response headers:** `x-uw-daily-req-count`, `x-uw-token-req-limit`, `x-uw-minute-req-counter`, `x-uw-req-per-minute-remaining`, `x-uw-req-per-minute-reset`

## Tiered Reasoning Pipeline

| Tier | Trigger | Model | Latency | Output |
|------|---------|-------|---------|--------|
| **1: Fast scan** | Every flow event (post-cooldown) | Haiku | ~300ms | Score 0–100, direction, log only |
| **2: Full analysis** | Tier 1 score ≥ threshold | Sonnet | ~3–5s | Directional alert → Discord |

### Tier 1: Fast Score

**Input:** Structured flow event data (ticker, side, premium, strike, expiry, volume, OI, underlying_price, flow_type).
**Output (structured):** `{ "score": int, "direction": "bullish"|"bearish"|"neutral", "reasoning": str }`
**Enforced via:** Anthropic `tool_use` structured output.
**Purpose:** Filter noise. ~500 events/day, most discarded.

### Tier 2: Full Analysis

**Input:** Flow event + underlying price context.
**Output (structured):** `{ "score": int, "direction": str, "confidence": float, "conviction_factors": [...], "reasoning": str }`
**Purpose:** Actionable directional alert. Phase 1a does not generate specific trade ideas (strike/expiry recommendations deferred).

## Configuration (MVP)

```yaml
# config/config.yaml

scheduler:
  market_hours_only: true
  poll_interval_seconds: 30

scoring:
  score_threshold: 75
  tier1_concurrency: 10
  tier1_timeout_seconds: 5
  tier2_timeout_seconds: 30
  alert_cooldown_seconds: 600    # per-ticker cooldown

models:
  tier1:
    provider: anthropic
    model: claude-haiku-4-5
  tier2:
    provider: anthropic
    model: claude-sonnet-4-6

uw_api:
  base_url: https://api.unusualwhales.com
  rate_limit_rpm: 120
  daily_limit: 15000
  retry_max: 3
  retry_backoff_base: 2

discord:
  # webhook_url via DISCORD_WEBHOOK_URL env var

health:
  enabled: true
  port: 8090

storage:
  db_path: data/signals.duckdb

logging:
  level: INFO
  format: json
  file: logs/scanner.log
  rotation: 10MB

ops:
  daily_spend_cap_usd: 10.0
```

**Secrets via env vars:** `UW_API_KEY`, `DISCORD_WEBHOOK_URL`, `ANTHROPIC_API_KEY`

## Data Model (MVP)

### flow_events
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Internal primary key |
| uw_event_id | VARCHAR | UW-provided UUID (dedup key) |
| ingested_at | TIMESTAMPTZ | When we received this event |
| tape_time | TIMESTAMPTZ | When UW recorded the flow |
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

**Indexes:** `UNIQUE (uw_event_id)`, `(ticker, tape_time)`, `(ingested_at)`

### signal_scores
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| flow_event_id | UUID | FK to flow_events |
| tier | INTEGER | 1 or 2 |
| model_used | VARCHAR | e.g., claude-haiku-4-5 |
| prompt_version | VARCHAR | e.g., v1.0.0 |
| score | INTEGER | 0–100 |
| direction | VARCHAR | bullish / bearish / neutral |
| confidence | DECIMAL | 0.0–1.0 (Tier 2 only) |
| reasoning | TEXT | LLM rationale |
| raw_output | JSON | Full structured LLM response |
| alert_key | VARCHAR | hash(flow_event_id + tier + prompt_version) |
| alert_status | VARCHAR | pending / sent / failed / skipped |
| created_at | TIMESTAMPTZ | Score timestamp |

**Indexes:** `(tier, score)`, `(flow_event_id)`, `UNIQUE (alert_key)`, `(alert_status, created_at)`

## Startup Validation

All required secrets and config are validated at process start via Pydantic settings. If any required env var is missing or malformed, the process exits immediately with a clear error — never silently fails on first API call during market hours.

**Validated at startup:**
- `UW_API_KEY` — non-empty string
- `ANTHROPIC_API_KEY` — non-empty string
- `DISCORD_WEBHOOK_URL` — valid URL format
- `config.yaml` — parseable, all required fields present
- DuckDB path — parent directory exists and is writable

## Spend Cap Enforcement

The `daily_spend_cap_usd` config value is enforced by a `SpendTracker` that:
1. Accumulates token usage from Anthropic API response metadata (`usage.input_tokens`, `usage.output_tokens`)
2. Converts to USD using configured per-token rates (updated when model pricing changes)
3. When cap is reached: skip Tier 2 scoring (continue Tier 1 with warning log), send a one-time Discord alert about budget exhaustion
4. Resets at midnight UTC

## DuckDB Concurrency

All DuckDB access is wrapped in `asyncio.to_thread()` to avoid blocking the event loop. Single connection per process — DuckDB is single-writer, so all writes are serialized. In Phase 2a, the backtester runs as a separate process after market close (not concurrent with the live scanner).

## Project Structure (MVP)

```
uw-flow-scanner/
├── config/
│   └── config.yaml
├── src/
│   ├── main.py              # entry point, scheduler loop
│   ├── config.py            # pydantic settings loader
│   ├── health.py            # /health endpoint
│   ├── poller.py            # UW API client + watermark + dedup
│   ├── scorer.py            # Tier 1/2 LLM scoring (structured output)
│   ├── prompts.py           # prompt templates (versioned)
│   ├── schemas.py           # pydantic models for LLM output
│   ├── discord.py           # webhook formatter + sender
│   └── db.py                # DuckDB init, insert, query
├── tests/
│   ├── test_poller.py
│   ├── test_scorer.py
│   ├── test_schemas.py
│   └── test_db.py
├── pyproject.toml
└── README.md
```

**Flat `src/` in MVP** — no subdirectories until complexity justifies it.

## Dependencies (MVP)

| Package | Purpose |
|---------|---------|
| `httpx` | Async HTTP for UW API |
| `anthropic` | Claude SDK (structured output via tool_use) |
| `duckdb` | Signal storage |
| `pydantic` | Config validation + LLM output schemas |
| `pandas-market-calendars` | RTH schedule, holidays |
| `structlog` | JSON logging |

**Not in MVP:** `tenacity` (simple retry loop instead), `openai` (no failover).

## Cost Estimate (MVP Monthly)

| Item | Cost |
|------|------|
| UW API Basic tier | $150/mo |
| Tier 1 Haiku (~10,500 events × 250 tokens) | ~$3/mo |
| Tier 2 Sonnet (~630 events × 1,000 tokens) | ~$4/mo |
| **Total** | **~$157/mo** |

## MVP Success Criteria

1. Scanner runs during market hours, polling every 30s
2. Every flow event gets a Tier 1 score within 1s
3. High-conviction signals (score ≥ 75) get Tier 2 analysis + Discord alert within 15s
4. All events + scores logged to DuckDB with prompt version
5. No duplicate Discord alerts
6. Daily LLM spend < $10
7. `/health` reports status and last poll timestamp

---

# Phase 1b: Resilience & Observability

**Trigger:** MVP runs for 1 week, proves it produces useful alerts.

## Additions

### Retry & Recovery
- **`scoring_jobs` table** — failed LLM calls create a job record. Recovery loop (every 5 min) retries jobs < 1 hour old.
- **`poll_log` table** — log every poll cycle (success/partial/failed, events fetched, rate limit state).

### LLM Failover
- Add `openai` as failover provider. Activates after 3 consecutive Anthropic failures.
- Separate config path from validation (which remains disabled).

### Heuristic Fallback
- Rule-based Tier 1 scorer for when both LLM providers are down.
- Scores based on: premium size (percentile), volume/OI ratio, OTM flag, sweep indicator.
- Lower confidence than LLM but keeps the pipeline alive.

### Flow Clustering
- Group events by `(ticker, 5-min window)` before scoring.
- Detects split orders that appear as multiple events.
- Scores the cluster, not individual events.
- **Ordering with cooldown:** events arrive → cluster by (ticker, 5-min window) → check cooldown on cluster's ticker → score if not in cooldown. Cooldown timer starts from when the last alert was *sent*, not from event arrival.

### Circuit Breaker
- UW API: pause 5 min after 5 consecutive failures.
- LLM: failover after 3 consecutive failures; heuristic after both providers fail.

### Observability
- Discord heartbeat at market open/close.
- `structlog` with automatic secret redaction.
- Log rotation (10MB).

### Updated Data Model

Add `scoring_jobs` table:

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| flow_event_id | UUID | FK to flow_events |
| status | VARCHAR | pending / in_progress / completed / failed / expired |
| tier | INTEGER | Target tier |
| retry_count | INTEGER | Number of attempts |
| created_at | TIMESTAMPTZ | Job creation time |
| updated_at | TIMESTAMPTZ | Last status change |
| error | TEXT | Error message if failed |

Add `poll_log` table:

| Column | Type | Description |
|--------|------|-------------|
| poll_id | UUID | Primary key |
| started_at | TIMESTAMPTZ | Poll cycle start |
| completed_at | TIMESTAMPTZ | Poll cycle end |
| status | VARCHAR | success / partial / failed |
| events_fetched | INTEGER | Total from API |
| events_new | INTEGER | After dedup |
| high_water_mark | TIMESTAMPTZ | Last seen tape_time (watermark for overlap window) |
| uw_daily_count | INTEGER | From response header |
| uw_minute_remaining | INTEGER | From response header |
| error | TEXT | Error if any |

### Minimal Backtester (Accuracy Baseline)

A minimal backtester is introduced in Phase 1b to establish the accuracy baseline needed before Phase 1c enrichment and Phase 2a's full analytics.

**`signal_outcomes` table:**

| Column | Type | Description |
|--------|------|-------------|
| signal_id | UUID | FK to signal_scores |
| ticker | VARCHAR | Symbol |
| predicted_direction | VARCHAR | bullish / bearish / neutral (denormalized from signal_scores) |
| entry_price | DECIMAL | Underlying price at signal time |
| price_1d | DECIMAL | Price after 1 trading day |
| price_5d | DECIMAL | Price after 5 trading days |
| outcome | VARCHAR | win / loss / scratch |
| updated_at | TIMESTAMPTZ | Last fill time |

**Indexes:** `UNIQUE (signal_id)`, `(outcome)`

**Nightly outcome filler:** After market close, fetches closing prices from UW API (`/stock/{T}/ohlc`) for signals 1 and 5 trading days old. Fills `price_1d`, `price_5d`, computes `outcome` using thresholds (win: ≥1% in predicted direction, scratch: ±0.5%, loss: ≥1% against).

**Accuracy query:** Simple SQL query computes directional hit rate by tier and prompt version. No full analyzer yet — that comes in Phase 2a.

### Cooldown Recovery on Restart

On startup, derive cooldown state from `signal_scores.created_at` — query the last alert time per ticker to restore cooldowns without a separate table.

### Updated Config Additions

```yaml
models:
  failover:
    provider: openai
    model: gpt-4o

resilience:
  uw_retries: 3
  uw_circuit_breaker_threshold: 5
  uw_circuit_breaker_pause_seconds: 300
  llm_retries: 2
  discord_retry_max: 3

scoring:
  flow_cluster_window_seconds: 300

backtest:
  win_threshold_pct: 1.0
  scratch_band_pct: 0.5
```

### Updated Dependencies

| Package | Purpose |
|---------|---------|
| `openai` | Failover LLM provider |
| `tenacity` | Retry policies + circuit breaker |

### Phase 1b Success Criteria

1. Scanner recovers automatically from any single dependency outage
2. No scoring jobs lost — failed calls retry within 1 hour
3. Poll log shows ≥99% successful polls during market hours
4. Heartbeat messages appear in Discord at open/close
5. Structured logs capture every error with context
6. After 1 week, `signal_outcomes` table has price fills and directional accuracy is measurable
7. Cooldown state restored correctly after process restart

---

# Phase 1c: Signal Enrichment

**Trigger:** Phase 1b stable for 1 week. Scoring accuracy baseline established.

## Goal

Inject contextual data into LLM prompts to improve scoring quality. Each enrichment addresses a known category of false positives or missed signals.

## New Endpoints

| Endpoint | Poll Cadence | Cache TTL | Purpose |
|----------|-------------|-----------|---------|
| `GET /earnings/ticker?ticker={T}` | On-demand (Tier 1) | 24h | Is this ticker reporting soon? |
| `GET /market/tide` | Every 60s | 60s | Market-wide call/put sentiment |
| `GET /stock/{T}/iv-rank` | On-demand (Tier 2) | 5 min | Is IV cheap or expensive? |
| `GET /market/economic-calendar` | Once at open | 24h | FOMC, CPI, jobs report today? |
| `GET /market/fda-calendar` | Once at open | 24h | FDA decisions today? |
| `GET /stock/{T}/max-pain` | On-demand (Tier 2/3) | 1h | Where's max pain vs. flow strike? |
| `GET /darkpool/{ticker}` | On-demand (Tier 3) | 5 min | Dark pool conviction signal |

**API budget impact:** ~1,500-2,000 additional calls/day → total ~4,000-5,000/day (well within 15K).

**Rate limit priority:** When approaching limits (tracked via `x-uw-req-per-minute-remaining`), enrichment calls are deferred or skipped in priority order: flow-alerts polling (highest — never skip) > market tide > earnings > IV rank / max pain / dark pool (lowest — skip first). A burst of 20 high-scoring events in one cycle could trigger 60+ enrichment calls — the rate limiter throttles these to stay within 120 rpm.

## Enrichment Integration

### Tier 1 Prompt Gets:
- **Earnings flag:** `"EARNINGS: TSLA reports in 2 days"` or `"EARNINGS: none upcoming"`
- **Market tide:** `"MARKET: net +$2.3B call premium (bullish)"` or `"MARKET: net -$800M (bearish)"`
- **Economic calendar:** `"EVENTS: FOMC decision today 2pm ET"` or `"EVENTS: none today"`

### Tier 2 Prompt Gets (all of Tier 1 plus):
- **IV rank:** `"IV_RANK: 82nd percentile (expensive)"`
- **Max pain:** `"MAX_PAIN: $175 (current price $182, flow strike $190)"`
- **FDA calendar:** `"FDA: no pending decisions for TSLA"` (pharma tickers only)

### Tier 3 Prompt Gets (all of Tier 2 plus):
- **Dark pool:** `"DARK_POOL: 3 large prints totaling $45M in last hour, 73% above ask"`
- **Recent flow summary:** `"RECENT_FLOW: 12 bullish events for TSLA today, $23M total premium"`

## Tier 3: Watchlist Deep Dives

Re-introduce Tier 3 with enrichment context:

| Tier | Trigger | Model | Latency | New in 1c |
|------|---------|-------|---------|-----------|
| **3: Watchlist** | Ticker in `watchlist.always_analyze` | Sonnet | ~5-8s | Dark pool + recent flow enrichment |

```yaml
watchlist:
  always_analyze: [SPX, SPY, QQQ]
```

### Per-Tier Discord Webhooks

Separate webhook URLs for Tier 2 vs Tier 3 alerts:

```yaml
discord:
  # DISCORD_WEBHOOK_URL — default
  # DISCORD_TIER2_WEBHOOK_URL — optional override
  # DISCORD_TIER3_WEBHOOK_URL — optional override
```

## Context Cache

New component: `ContextCache` — in-memory cache for enrichment data with per-key TTLs.

```python
class ContextCache:
    """In-memory cache with per-key TTL for enrichment data."""
    # earnings: ticker → {reports_date, days_until} — TTL 24h
    # market_tide: single key → {net_premium, direction} — TTL 60s
    # calendar: single key → [events] — TTL 24h
    # iv_rank: ticker → {rank, percentile} — TTL 5min
    # max_pain: ticker → {strike, expiry} — TTL 1h
    # darkpool: ticker → {prints, total_volume, conviction} — TTL 5min
```

**Startup pre-warming:** On process start (before first scoring cycle), pre-fetch all long-TTL data (earnings for watchlist tickers, economic calendar, FDA calendar). This avoids a burst of API calls on the first poll cycle and ensures enrichment is available immediately.

### Updated Project Structure

```
src/
├── main.py
├── config.py
├── health.py
├── poller.py
├── scorer.py
├── prompts.py           # now has tier1/tier2/tier3 templates with enrichment slots
├── schemas.py
├── discord.py
├── db.py
├── enrichment/          # NEW
│   ├── cache.py         # ContextCache
│   ├── earnings.py      # earnings proximity check
│   ├── market.py        # market tide + calendars
│   ├── volatility.py    # IV rank
│   ├── darkpool.py      # dark pool data
│   └── max_pain.py      # max pain levels
└── resilience/
    └── retry.py         # from Phase 1b
```

### Phase 1c Cost Impact

| Item | Additional Cost |
|------|----------------|
| UW API calls (+1,500/day) | $0 (same $150/mo tier) |
| Tier 3 Sonnet (~420 events/mo) | ~$4/mo |
| Enriched Tier 1 prompts (larger input, ~2x tokens) | ~$3/mo |
| **Phase 1c total** | **~$164/mo** |

### Phase 1c Success Criteria

1. Zero false positives on earnings flow (earnings flag suppresses or contextualizes)
2. Alerts include market tide context in Discord embeds
3. Tier 3 watchlist tickers get dark pool + flow summary enrichment
4. Economic calendar events noted in Discord alerts on event days
5. Scoring accuracy (directional) improves vs. Phase 1b baseline

---

# Phase 2a: Unique Alpha Signals

**Trigger:** Phase 1c runs for 2+ weeks. Phase 1b backtester shows positive directional accuracy (>50% hit rate on Tier 2 signals).

## New Signal Sources

### Congress Trading Poller

Congressional trades are rare (~5-15/day), high-signal, and unique to UW. They bypass Tier 1 entirely — any congressional trade on a watchlist ticker goes straight to Tier 3.

**Endpoints:**

| Endpoint | Poll Cadence | Purpose |
|----------|-------------|---------|
| `GET /congress/recent-trades` | Every 30 min | New congressional disclosures |
| `GET /congress/trader?name={N}` | On-demand | Specific politician's history |

**Routing:**
- Congress trade on a watchlist ticker → Tier 3 (always)
- Congress trade on any other ticker → Tier 2 (score it)
- Dedup by disclosure date + ticker + politician + transaction_type + amount_range (no UUID from API)

**Discord:** Separate embed style with politician name, party, trade type (buy/sell), amount range, and ticker.

### Insider Trading Correlation

**Endpoint:** `GET /insiders/ticker-flow?ticker={T}`
**Use:** On-demand during Tier 2/3 scoring. Check if insiders bought/sold recently.
**Prompt injection:** `"INSIDERS: CEO sold 50,000 shares on 2026-03-05 ($8.2M)"`

### GEX Context

**Endpoints:**
- `GET /stock/{T}/greek-exposure` — aggregate GEX
- `GET /stock/{T}/spot-gex-exposures-1min` — real-time dealer positioning

**Use:** Tier 2/3 enrichment. Identify GEX flip points, resistance/support walls, dealer positioning bias.
**Prompt injection:** `"GEX: negative gamma below $178, flip point at $180, call wall at $185"`

### Seasonality

**Endpoint:** `GET /seasonality/monthly-returns?ticker={T}`
**Use:** Tier 2/3 enrichment. Cheap one-liner.
**Prompt injection:** `"SEASONALITY: SPY historically +2.3% in March (78% win rate over 20 years)"`

## Full Backtester Analytics

Expand the Phase 1b minimal backtester into a comprehensive analysis tool.

### signal_outcomes table (expanded from Phase 1b)

Add columns to the existing `signal_outcomes` table:

| Column | Type | Description |
|--------|------|-------------|
| price_3d | DECIMAL | Price after 3 trading days (new) |
| price_10d | DECIMAL | Price after 10 trading days (new) |
| max_favorable | DECIMAL | Best price in evaluation window (new) |
| max_adverse | DECIMAL | Worst price in evaluation window (new) |

### Backtester Analyzer

Full analyzer (separate process, runs after market close):
- Directional accuracy by tier, prompt version, ticker, and time period
- Score calibration curves (do higher scores predict better outcomes?)
- Prompt version comparison (A/B testing across prompt versions)
- Excludes time windows with failed/partial polls (from `poll_log`)

### Prompt Version Registry

Move prompts to versioned YAML files:

```
prompts/
├── tier1/
│   ├── v1.0.0.yaml
│   └── v1.1.0.yaml   # with enrichment slots
├── tier2/
│   └── v1.0.0.yaml
└── tier3/
    └── v1.0.0.yaml
```

Config selects active version per tier. Backtester correlates accuracy by prompt version.

## Discord Bot (Phase 2a)

Upgrade from webhook to Discord.py bot for interactive features.

### Bot Commands

| Command | Action |
|---------|--------|
| `/scan {ticker}` | Trigger on-demand Tier 2/3 analysis |
| `/watchlist add {ticker}` | Add ticker to Tier 3 watchlist |
| `/watchlist remove {ticker}` | Remove from watchlist |
| `/watchlist show` | List current watchlist |
| `/stats` | Today's alert count, hit rate, API usage |
| `/status` | Scanner health, last poll, uptime |

### Reaction Tracking

- React with a checkmark on an alert = "I took this trade"
- Bot tracks which alerts were acted on
- Weekly summary: "You acted on 12 alerts, 8 were directional wins (67%)"

### Architecture Change

```
Discord Webhook (Phase 1a-1c)  →  Discord Bot (Phase 2a)
  - Fire and forget                - Bidirectional
  - Single webhook URL             - Bot token + guild permissions
  - No user interaction            - Commands, reactions, threads
```

**Library:** `discord.py` (async, fits the existing asyncio architecture).

### Phase 2a Cost Impact

| Item | Additional Cost |
|------|----------------|
| Congress/insider/GEX API calls | $0 (same tier) |
| Additional Tier 2/3 scoring | ~$3/mo |
| **Phase 2a total** | **~$165/mo** |

### Phase 2a Success Criteria

1. Congressional trades surface in Discord within 30 min of UW ingestion
2. Backtester produces accuracy stats after 2 weeks of accumulation
3. Prompt version changes measurably affect accuracy metrics
4. Discord bot responds to commands with <2s latency
5. Insider + GEX context visible in Tier 2/3 Discord embeds

---

# Phase 2b: Architecture Upgrade

**Trigger:** System is profitable. WebSocket ROI justified by alpha generation.

## FlowSource Abstraction

Decouple ingestion from transport:

```python
class FlowSource(ABC):
    async def stream(self) -> AsyncIterator[FlowEvent]: ...

class PollingFlowSource(FlowSource):
    """Phase 1: httpx + watermark polling"""

class WebSocketFlowSource(FlowSource):
    """Phase 2b: wss://api.unusualwhales.com/socket"""
```

## WebSocket Channels

**Requires Advanced tier ($375/mo)**

| Channel | Purpose |
|---------|---------|
| `flow-alerts` | Real-time flow alerts (replaces polling) |
| `off_lit_trades` | Dark pool prints (eliminates 15-min delay) |
| `price:TICKER` | Live price for watchlist tickers |
| `gex:TICKER` | Real-time GEX updates |

**Protocol:**
- Connect: `wss://api.unusualwhales.com/socket?token=<TOKEN>`
- Subscribe: `{"channel":"flow-alerts","msg_type":"join"}`
- Data: `["flow-alerts", <payload>]`

## MCP Server Integration

Use UW's official MCP server for on-demand enrichment queries (alternative to raw REST for Tier 2/3 context gathering).

## APEX Adapter

Feed scored signals into APEX risk engine:
- APEX adapter reads from DuckDB or REST endpoint
- UW signals become inputs to portfolio exposure checks
- Options P&L tracking: greeks at entry, IV context, strategy payoff

## Options P&L Tracking

Upgrade from directional accuracy to true options P&L:
- Record option greeks at signal time
- Track theoretical strategy payoff (not just underlying move)
- Tier 2/3 output includes specific trade ideas (strike/expiry/strategy)

### Phase 2b Cost Impact

| Item | Cost Change |
|------|-------------|
| UW Advanced tier | $375/mo (was $150) |
| LLM costs (same as 2a) | ~$15/mo |
| **Phase 2b total** | **~$390/mo** |

---

# Cross-Phase Reference

## Full Endpoint Usage by Phase

| Phase | Endpoints | Daily API Calls | Budget Used |
|-------|-----------|----------------|-------------|
| 1a | 1 (`flow-alerts`) | ~2,000 | 13% |
| 1b | 1 (same) | ~2,000 | 13% |
| 1c | 7 (+earnings, tide, IV, calendars, max pain, darkpool) | ~4,500 | 30% |
| 2a | 12 (+congress, insiders, GEX, seasonality) | ~5,500 | 37% |
| 2b | WebSocket (unlimited) + REST enrichment | ~2,000 REST | 13% |

## Dependency Graph

| Package | 1a | 1b | 1c | 2a | 2b |
|---------|----|----|----|----|-----|
| httpx | x | x | x | x | x |
| anthropic | x | x | x | x | x |
| duckdb | x | x | x | x | x |
| pydantic | x | x | x | x | x |
| pandas-market-calendars | x | x | x | x | x |
| structlog | x | x | x | x | x |
| openai | | x | x | x | x |
| tenacity | | x | x | x | x |
| discord.py | | | | x | x |
| websockets | | | | | x |

## Monthly Cost Progression

| Phase | UW API | LLM | Other | Total |
|-------|--------|-----|-------|-------|
| 1a | $150 | $7 | $0 | $157 |
| 1b | $150 | $9 | $0 | $159 |
| 1c | $150 | $14 | $0 | $164 |
| 2a | $150 | $15 | $0 | $165 |
| 2b | $375 | $15 | $0 | $390 |

## Operations

### Deployment (All Phases)
- Single process, `systemd` (Linux) or `launchd` (macOS)
- Graceful shutdown: `SIGTERM` handler drains current poll cycle
- Startup recovery (Phase 1b+): scan `scoring_jobs` for stale `in_progress`, reset to `pending`

### Retention
- DuckDB data retained 90 days (configurable)
- Nightly cleanup: `DELETE WHERE tape_time < now() - interval '90 days'`

### Secrets (env vars only)
- `UW_API_KEY` — Unusual Whales API token
- `ANTHROPIC_API_KEY` — Claude API key
- `OPENAI_API_KEY` — GPT API key (Phase 1b+)
- `DISCORD_WEBHOOK_URL` — Default webhook (Phase 1a-1c)
- `DISCORD_BOT_TOKEN` — Bot token (Phase 2a+)
- `DISCORD_TIER2_WEBHOOK_URL` — Optional tier-specific webhook (Phase 1c+)
- `DISCORD_TIER3_WEBHOOK_URL` — Optional tier-specific webhook (Phase 1c+)

---

# Tribunal Review Log

**Original reviewers:** Codex gpt-5.4 + Claude (bilateral mode)

### Changes from Original Monolithic Spec

| # | Change | Rationale |
|---|--------|-----------|
| 1 | Split into 5 phases (1a/1b/1c/2a/2b) | Ship MVP fast, prove value before investing |
| 2 | MVP uses 1 endpoint, 2 tables | Minimum viable loop |
| 3 | Dropped clustering from MVP | Simplify — cooldown is sufficient initially |
| 4 | Added earnings awareness (1c) | Prevents false positives around earnings |
| 5 | Added market tide context (1c) | Contextualizes signals vs. market sentiment |
| 6 | Added economic/FDA calendars (1c) | Suppresses noise around scheduled catalysts |
| 7 | Added congress trading poller (2a) | Unique alpha signal, low noise, UW differentiator |
| 8 | Added insider trading correlation (2a) | Confirming/conflicting signal for Tier 2/3 |
| 9 | Added GEX context enrichment (2a) | Dealer positioning context improves scoring |
| 10 | Added Discord bot upgrade path (2a) | Interactive commands, reaction tracking, trade journaling |
| 11 | Added FlowSource abstraction (2b) | Clean WebSocket upgrade path |
| 12 | Added prompt version registry (2a) | A/B test prompts via backtester |
| 13 | Flat `src/` structure in MVP | No premature directory nesting |
| 14 | Added ContextCache (1c) | Efficient enrichment data management with TTLs |
| 15 | Added seasonality endpoint (2a) | Cheap enrichment for Discord embeds |

### Architect Review (Rev 2) — Issues Addressed

| # | Issue | Severity | Fix |
|---|-------|----------|-----|
| 1 | Backtester in Phase 2a but needed to measure Phase 1c accuracy | Critical | Moved minimal backtester to Phase 1b |
| 2 | `alert_sent` boolean insufficient for debugging | Major | Changed to `alert_status` VARCHAR enum |
| 3 | No startup secret validation | Major | Added startup validation section |
| 4 | Spend cap has no enforcement mechanism | Major | Added SpendTracker component |
| 5 | DuckDB concurrency model unaddressed | Major | Added concurrency section (asyncio.to_thread) |
| 6 | No rate limit handling for enrichment burst | Major | Added rate limit priority ordering in Phase 1c |
| 7 | `poll_log.high_water_mark` type mismatch | Minor | Changed to TIMESTAMPTZ |
| 8 | No index on `flow_events.ingested_at` | Minor | Added index |
| 9 | `signal_outcomes` missing `predicted_direction` | Minor | Added denormalized column |
| 10 | Cooldown vs. clustering interaction undefined | Minor | Documented ordering |
| 11 | Congress trade dedup key too narrow | Minor | Added transaction_type + amount_range |
| 12 | ContextCache needs pre-warming | Minor | Added startup pre-warming note |
| 13 | Phase 1c enriched prompt cost underestimated | Minor | Revised to ~$3/mo |
| 14 | Phase 2b LLM cost reduction unjustified | Minor | Removed false savings claim |
| 15 | Cooldown state lost on restart | Minor | Added recovery from signal_scores on startup |
