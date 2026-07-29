"""What a pass actually costs, in money.

`/second-brain-stats` reports tokens because tokens are what the provider returns,
but tokens are not the question anyone is asking — "is this worth leaving on?" is.
So the same numbers are priced here.

Two things this deliberately does not do. It does not guess: a model with no entry
in the table prices as unknown and the command says so, rather than quoting a
confident wrong number. And it does not decide anything — nothing in the loop reads
a price. The budget guard counts tokens (§Budget guard) because a token ceiling is
enforceable without a pricing table being current, and a stale table must never be
able to silence the observer.

Rates are per million tokens. Cache multipliers follow the published model: a read
costs ~0.1× the base input rate, a 5-minute write 1.25×, a 1-hour write 2×.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}

# model id → (input $/MTok, output $/MTok). Only models this plugin would
# plausibly run on; the observer is meant to be the cheap tier.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
}


@dataclass
class Cost:
    """A priced token bucket. `known` is false when the model is not in the table."""

    input: float = 0.0
    output: float = 0.0
    cache_write: float = 0.0
    cache_read: float = 0.0
    known: bool = True

    @property
    def total(self) -> float:
        return self.input + self.output + self.cache_write + self.cache_read

    def render(self) -> str:
        if not self.known:
            return "unpriced (model not in the rate table)"
        return (f"${self.total:.4f}  (in ${self.input:.4f} · out ${self.output:.4f} · "
                f"cache write ${self.cache_write:.4f} · cache read ${self.cache_read:.4f})")


def rates(model: str, override_in: float = 0.0, override_out: float = 0.0) -> tuple[float, float] | None:
    """Per-MTok rates for a model, or None if it is unknown and unpriced."""
    if override_in > 0 and override_out > 0:
        return override_in, override_out
    if model in PRICES:
        return PRICES[model]
    # A dated snapshot of a known model prices as that model.
    for known, price in PRICES.items():
        if model.startswith(known):
            return price
    return None


def estimate(model: str, tokens: dict[str, Any], *, cache_ttl: str = "5m",
             override_in: float = 0.0, override_out: float = 0.0) -> Cost:
    """Price a `{input, output, cache_read, cache_write}` token count."""
    found = rates(model, override_in, override_out)
    if found is None:
        return Cost(known=False)
    price_in, price_out = found
    write_multiplier = CACHE_WRITE_MULTIPLIER.get(cache_ttl, 1.25)
    per_token_in = price_in / 1_000_000
    return Cost(
        input=int(tokens.get("input", 0)) * per_token_in,
        output=int(tokens.get("output", 0)) * price_out / 1_000_000,
        cache_write=int(tokens.get("cache_write", 0)) * per_token_in * write_multiplier,
        cache_read=int(tokens.get("cache_read", 0)) * per_token_in * CACHE_READ_MULTIPLIER,
    )


def per_hour(cost: float, elapsed_s: float, passes: int = 0) -> float | None:
    """Extrapolate a run rate, or None when the sample cannot support one.

    Two guards, both learned from a real reading. Wall-clock under five minutes is
    too short to divide by. And **one pass is not a rate**: the first pass of a task
    is systematically atypical — it writes the prefix cache from cold and often
    carries a larger episode — so extrapolating from it reports a number several
    times the steady state. Refusing to answer is better than answering wrongly.
    """
    if elapsed_s < 300 or passes < 2:
        return None
    return cost * 3600.0 / elapsed_s
