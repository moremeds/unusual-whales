from __future__ import annotations

PROMPT_VERSION = "v1.0.0"

TIER1_SYSTEM = """\
You are an options flow analyst. \
Score unusual options activity on a 0-100 scale \
for directional conviction. Focus on:
- Premium size relative to typical flow
- Volume vs open interest ratio (high = new position)
- Sweep vs block (sweeps = urgency)
- OTM vs ITM (OTM = speculative)
- Side (call = bullish, put = bearish) combined with sentiment"""

TIER1_USER_TEMPLATE = """\
Score this options flow event:

Ticker: {ticker}
Side: {side}
Sentiment: {sentiment}
Flow Type: {flow_type}
Premium: ${premium:,.0f}
Strike: ${strike}
Expiry: {expiry}
Volume: {volume:,}
Open Interest: {open_interest:,}
Underlying Price: ${underlying_price}

Provide a score (0-100), direction (bullish/bearish/neutral), \
and brief reasoning."""

TIER2_SYSTEM = """\
You are a senior options flow analyst providing detailed \
directional analysis. Evaluate the flow event considering:
- Absolute premium size and relative significance
- Volume/OI ratio and what it implies about position initiation
- Flow type characteristics (sweep = aggressive, block = institutional)
- Strike selection relative to current price
- Expiry timeframe and what it implies about expected move timing
- Overall sentiment signal strength

Provide high-conviction directional analysis with clear reasoning."""

TIER2_USER_TEMPLATE = """\
Analyze this high-scoring options flow event in detail:

Ticker: {ticker}
Side: {side}
Sentiment: {sentiment}
Flow Type: {flow_type}
Premium: ${premium:,.0f}
Strike: ${strike}
Expiry: {expiry}
Volume: {volume:,}
Open Interest: {open_interest:,}
Underlying Price: ${underlying_price}

Provide a detailed directional analysis with score (0-100), \
direction, confidence (0.0-1.0), conviction factors, \
and reasoning."""


def format_tier1_prompt(event_data: dict) -> str:
    return TIER1_USER_TEMPLATE.format(**event_data)


def format_tier2_prompt(event_data: dict) -> str:
    return TIER2_USER_TEMPLATE.format(**event_data)
