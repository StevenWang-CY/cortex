"""Anthropic per-token pricing table + provider model-id normaliser.

Single source of truth for the per-call USD cost telemetry emitted by
:mod:`cortex.services.llm_engine.cost_tracker`. Prices are the Anthropic
first-party list prices in USD per million tokens (input / output). The
Bedrock Mantle and Vertex transports bill the same per-token rates for
these models, so one table serves all three providers; update it when
Anthropic publishes new list prices.

Cache accounting follows Anthropic's published multipliers:

* prompt-cache **reads** bill at ``0.1x`` the input rate;
* five-minute ephemeral cache **writes** bill at ``1.25x`` the input rate.

:func:`usd_cost` encodes both multipliers so callers never have to.
Provider-specific identifiers (``anthropic.claude-sonnet-5``,
``us.anthropic.claude-sonnet-4-6-v1:0``, ``claude-haiku-4-5@20251001``)
are folded back onto the Cortex logical id by :func:`normalize_model_id`;
an id that does not normalise to a priced tier raises ``KeyError`` so
cost telemetry never silently drops a model.
"""

from __future__ import annotations

import re
from typing import Final

# Per-million-token prices (USD) keyed by the Cortex logical model ids
# declared in :data:`cortex.libs.config.settings.LogicalModelId`.
# ``cortex/tests/unit/test_llm_pricing.py`` asserts the key set matches
# the literal and the capability/provider tables exactly.
PRICES_USD_PER_MTOK: Final[dict[str, tuple[float, float]]] = {
    # (input_per_mtok, output_per_mtok)
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Legacy, still-served tiers.
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}

# Cache reads bill at 10% of the input rate.
CACHE_READ_MULTIPLIER: Final[float] = 0.1
# Ephemeral (5-minute) cache writes bill at 1.25x the input rate.
CACHE_WRITE_MULTIPLIER: Final[float] = 1.25

# ``anthropic.`` (Bedrock Mantle), ``us.anthropic.`` / ``eu.anthropic.`` /
# ``global.anthropic.`` (legacy Bedrock cross-region inference profiles).
_PROVIDER_PREFIX: Final[re.Pattern[str]] = re.compile(r"^(?:[a-z]{2,6}\.)?anthropic\.")
# Legacy Bedrock InvokeModel revision suffix (``-v1:0``).
_BEDROCK_REVISION_SUFFIX: Final[re.Pattern[str]] = re.compile(r"-v\d+:\d+$")
# Dated snapshot suffix some legacy ids carry (``-20251001``).
_DATE_SUFFIX: Final[re.Pattern[str]] = re.compile(r"-\d{8}$")


def normalize_model_id(model_id: str) -> str:
    """Map any provider-specific model identifier to its logical tier id.

    Strips the Bedrock ``anthropic.`` / ``us.anthropic.`` prefixes, the
    legacy ``-v1:0`` revision suffix, a Vertex ``@date`` revision, and a
    dated ``-YYYYMMDD`` snapshot suffix. Logical ids pass through.

    Raises:
        KeyError: when the identifier does not normalise to a known
            Cortex model tier. The message preserves the original
            ``model_id`` so the cost tracker can log the offending value.
    """
    raw = (model_id or "").strip()
    raw = _PROVIDER_PREFIX.sub("", raw)
    raw = raw.split("@", 1)[0]
    raw = _BEDROCK_REVISION_SUFFIX.sub("", raw)
    raw = _DATE_SUFFIX.sub("", raw)
    if raw in PRICES_USD_PER_MTOK:
        return raw
    raise KeyError(f"Unknown LLM model id for pricing: {model_id!r}")


def usd_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Compute the USD cost of a single LLM call.

    Args:
        model_id: Provider-specific or logical model identifier. Bedrock,
            Vertex, and direct names all resolve to the same logical tier
            via :func:`normalize_model_id`.
        input_tokens: Non-cached prompt tokens billed at the input rate.
        output_tokens: Completion tokens billed at the output rate.
        cache_read: Tokens served from the prompt cache, billed at
            ``0.1x`` the input rate.
        cache_write: Tokens written to the ephemeral cache, billed at
            ``1.25x`` the input rate.

    Returns:
        Estimated USD cost, never negative.

    Raises:
        KeyError: when ``model_id`` is unknown.
        ValueError: when any token count is negative.
    """
    if input_tokens < 0 or output_tokens < 0 or cache_read < 0 or cache_write < 0:
        raise ValueError(
            "Token counts must be non-negative; got "
            f"in={input_tokens} out={output_tokens} "
            f"cache_read={cache_read} cache_write={cache_write}",
        )
    logical = normalize_model_id(model_id)
    input_rate, output_rate = PRICES_USD_PER_MTOK[logical]
    cost = (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_read * input_rate * CACHE_READ_MULTIPLIER
        + cache_write * input_rate * CACHE_WRITE_MULTIPLIER
    ) / 1_000_000.0
    return float(cost)


__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER",
    "PRICES_USD_PER_MTOK",
    "normalize_model_id",
    "usd_cost",
]
