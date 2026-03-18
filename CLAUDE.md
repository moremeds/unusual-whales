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

## Development Commands

```bash
# Install (from worktree or project root with pyproject.toml)
pip install -e ".[dev]"

# Run the scanner
uw-scanner                    # CLI entry point (uw_flow_scanner.main:cli)

# Tests
pytest                        # all tests (asyncio_mode=auto, no markers needed)
pytest tests/test_poller.py   # single file
pytest -k "test_watermark"    # single test by name

# Lint
ruff check .                  # lint (E, F, I, N, W rules)
ruff format .                 # format
```

## Project Structure

```
uw_flow_scanner/
├── main.py                # entry point, scheduler loop, signal handling
├── core/
│   ├── config.py          # pydantic-settings: YAML + env vars, secret validation
│   ├── schemas.py         # pydantic models: UW API response, LLM output (Tier 1/2)
│   └── db.py              # DuckDB: table init, insert, query (asyncio.to_thread)
├── ingestion/
│   └── poller.py          # UW API client: httpx async, rate limits, watermark dedup
├── scoring/
│   ├── prompts.py         # versioned prompt templates for Tier 1/2
│   └── scorer.py          # Anthropic tool_use structured output, tier routing
├── alerting/
│   └── discord.py         # webhook: rich embeds, idempotent via alert_key
└── health/
    └── server.py          # /health JSON endpoint (asyncio TCP)
tests/
├── conftest.py            # shared fixtures: temp DuckDB, mock UW/Anthropic responses
├── test_*.py              # one test file per module
config/config.yaml         # runtime config (thresholds, models, intervals)
```

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


## Coding Standards

- Python 3.12+, 4-space indent, 100-char line length (ruff enforced)
- 你不要保留任何旧代码和数据，这只会干扰你今后的重构（非常重要）
- Type hints required on all functions (mypy strict)
- No backward compat required — freely refactor/rename/delete
- No over-defensive coding — trust internal code, skip impossible-case guards
- `try/except` always logs the error
- Use typed dataclasses for return values (not `Dict[str, Any]`)
- Keep files small; split when complexity grows

## Guardrails (CRITICAL)

- **Wire all features** — never leave code dead/unconnected. Every feature callable from runners/TUI/main.py
- **Enable by default** — no accumulating dead code behind flags
- **Merge, don't duplicate** — extend existing functions instead of parallel implementations
- **Validate before proceeding** — run scripts to verify; define success criteria per step
- **Unit test time logic** — DST, market hours boundaries need deterministic tests
- **Never commit without explicit request** — draft messages but don't run `git commit`
- **Naming**: `get_*` (from cache), `fetch_*` (network/disk), `load_*` (deserialize from file/DB)
- **Unwired >1 sprint**: delete (preferred) or move to `experimental/` with flag
- **Fix every issue you spot — no exceptions** — if you find a bug, the user reports one, or a reviewer (Codex, Gemini, `/codex-review`) flags one: fix it. No dismissing as "pre-existing", "out of scope", or "not in this PR". Whether you caused it or not, whether it's in changed code or untouched code — if you see it, you own it. Fix before declaring done
- **Test mocking:** use `respx` for httpx mocking, not `unittest.mock.patch` on HTTP calls

## Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

### 7. Codex Tribunal Review Gate (Mandatory)

**Every artifact (code, plans, designs) must pass through `/codex-review` before being finalized.**

**Skill location:** `~/.claude/skills/codex-review/` (global, works across all projects)
**Requires:** Codex CLI (`codex-cli`) + Gemini CLI (`gemini`). Codex uses gpt-5.3-codex (or gpt-5.4 for architecture reviews).

#### How It Works

1. **Context detection** — auto-detects what to review: args > uncommitted diff > active PR > recent plan files > ask user
2. **Phase 1: Independent reviews (parallel)** — Codex (coding specialist, weight 1.0), Gemini (wide-context, weight 0.5), and Claude (codebase-aware, weight 1.0) each produce their own issue list simultaneously
3. **Phase 2: Weighted merge** — Claude deduplicates and calculates agreement weight: ≥1.5 → consensus, <1.5 → debate
4. **Phase 3: Consensus items** — no debate needed for items with sufficient weight agreement
5. **Phase 4: Debate & Rebuttal** — contested items get two structured exchanges: (A) Debate: models challenge opposing positions with counter-evidence and attack vectors, (B) Rebuttal: original position holders defend with NEW evidence or concede
6. **Phase 5: Final output** — Consensus + Unresolved (user decides) + Dismissed + Stats

**Weight rules:** Codex+Claude=2.0 (consensus), Trusted+Gemini=1.5 (consensus), Gemini alone=0.5 (needs debate)
**Gemini strategy:** Feed it MORE context (up to 50 files) — its 1M+ token window is its main advantage
**Graceful degradation:** 3-way → 2-way (if either unavailable) → Claude-only

#### Rules

- **ALWAYS WAIT for results:** When `/codex-review` is invoked, BLOCK until ALL results are returned. Do NOT take any next action (merge, commit, claim done, move to next step) while review is in progress. Reviews can take 3-10+ minutes — that is normal. If you don't wait, the review is pointless.
- **Skip when:** trivial single-line fixes, pure research/exploration, Codex CLI not installed
- **If critical/important issues found:** fix before proceeding
- **If unresolved disagreements:** present all three positions, user decides
- **Bootstrap exception:** when modifying `codex-review` itself, use direct `codex exec review` instead of the full skill loop
- **Failure fallback:** gracefully degrade 3-way → 2-way → Claude-only — never block development

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

## Claude Code Meta

- **Self-evolution injection** — when a session involves >8 tool calls for a repeating pattern, output an optimization suggestion showing how to distill the operation into a reusable Skill
- **Strategic compression** — proactively `/compact` at each task boundary to maintain high signal-to-noise ratio in context; don't wait for context to overflow
- **Hacker perspective review** — before deployment, review code from an attacker's perspective (injection, auth bypass, data leaks); run `/security` or equivalent audit

DONT COMMIT CODE UNLESS BEING INSTRUCTED TO DO SO