"""Per-model token prices and cost arithmetic.

Prices are per million tokens in the settings currency (USD by default).
`LLMSettings.prices` overrides or extends the defaults. A model with no
price costs 0 and the provider warns once, so spend caps cannot see it.

Anthropic prices are first-party API rates (cached 2026-09-25); cache
writes are 1.25x input (5-minute TTL). OpenAI ships no defaults: its
current price list could not be verified when this was written, so set
`prices` for the models you use.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """Price per million tokens. `cache_write` defaults to `input` when 0."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(1.00, 5.00, 0.10, 1.25),
    "claude-sonnet-5-5": ModelPrice(2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-5": ModelPrice(2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00, 0.30, 3.75),
    "claude-opus-5-5": ModelPrice(4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": ModelPrice(5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": ModelPrice(5.00, 25.00, 0.50, 6.25),
    "claude-fable-5-1": ModelPrice(10.00, 50.00, 0.25, 12.50),
}


def price_for(model: str, overrides: dict[str, ModelPrice] | None = None) -> ModelPrice | None:
    """Exact match first, then the longest known prefix (dated snapshots)."""
    table = {**DEFAULT_PRICES, **(overrides or {})}
    if model in table:
        return table[model]
    best = ""
    for name in table:
        if model.startswith(name) and len(name) > len(best):
            best = name
    return table[best] if best else None


def cost_of(
    price: ModelPrice | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """`input_tokens` excludes cache reads and writes; each is billed at its own rate."""
    if price is None:
        return 0.0
    write = price.cache_write or price.input
    read = price.cache_read or price.input
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * read
        + cache_write_tokens * write
    )
    return total / 1_000_000
