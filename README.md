# UW Flow Scanner

Near-real-time options flow scanner using Unusual Whales API with LLM scoring.

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env  # fill in your API keys
```

## Run

```bash
uw-scanner                    # uses config/config.yaml
uw-scanner path/to/config.yaml  # custom config
```

## Test

```bash
pytest tests/ -v
```

## Architecture

Poll UW API → Tier 1 score (Haiku) → Tier 2 analysis (Sonnet) if score ≥ 75 → Discord alert → DuckDB log.

See `docs/superpowers/specs/2026-03-12-uw-flow-scanner-phased-design.md` for full design.
