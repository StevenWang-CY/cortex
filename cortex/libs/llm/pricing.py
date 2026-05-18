"""Anthropic / Bedrock per-token pricing table (audit F20).

Single source of truth for the per-call USD cost telemetry emitted by
:mod:`cortex.services.llm_engine.cost_tracker`. Prices are sourced from
the public AWS Bedrock on-demand pricing page (us-east-2,
``us.anthropic.*`` cross-region inference profiles) and mirrored for the
direct-Anthropic and Vertex paths since their per-token rates match the
Bedrock list price as of v0.2.x. Update the table when AWS publishes new
inference-profile pricing.

Units are USD per million tokens. Cache reads bill at the same input
rate; cache writes bill at the higher five-minute ephemeral rate, which
for the current Anthropic SDK is 1.25x the base input rate. ``usd_cost``
encodes that multiplier so callers do not have to.
"""

from __future__ import annotations

from typing import Final

# Per-million-token prices (USD). Anchored to the Cortex logical model
# IDs declared in :mod:`cortex.libs.llm.anthropic_client`. New logical
# tiers must add an entry here or :func:`usd_cost` raises ``KeyError``
# — the loud failure is intentional so cost telemetry never silently
# drops a model.
_PRICES_USD_PER_MTOK: Final[dict[str, tuple[float, float]]] = {
    # (input_per_mtok, output_per_mtok)
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-7": (15.0, 75.0),
}

# Ephemeral (5-minute) cache writes bill at 1.25x the base input rate per
# Anthropic's published pricing.
_CACHE_WRITE_MULTIPLIER: Final[float] = 1.25

# Logical-model lookup keys we accept as aliases for the provider-specific
# identifiers (Bedrock inference profile IDs, Vertex revisions, direct
# names). Maps any of those identifiers back to the logical key.
_PROVIDER_TO_LOGICAL: Final[dict[str, str]] = {
    # Bedrock cross-region inference profiles.
    "us.anthropic.claude-sonnet-4-6-v1:0": "claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-v1:0": "claude-haiku-4-5",
    "us.anthropic.claude-opus-4-7-v1:0": "claude-opus-4-7",
    # Vertex revisions.
    "claude-sonnet-4-6@20251015": "claude-sonnet-4-6",
    "claude-haiku-4-5@20251001": "claude-haiku-4-5",
    "claude-opus-4-7@20251101": "claude-opus-4-7",
    # Direct: the logical name passes through unchanged but we still want
    # the lookup to be explicit.
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-haiku-4-5": "claude-haiku-4-5",
    "claude-opus-4-7": "claude-opus-4-7",
}


def _logical_model(model_id: str) -> str:
    """Map a provider-specific model identifier to its logical tier name.

    Raises:
        KeyError: when the identifier is not a recognised Cortex model.
            The exception preserves the original ``model_id`` so the
            cost-tracker can log the offending value.
    """
    if model_id in _PROVIDER_TO_LOGICAL:
        return _PROVIDER_TO_LOGICAL[model_id]
    # Allow ``claude-foo-X-Y`` style direct names that already match the
    # logical key — the LogicalModel literal in ``settings.py``.
    if model_id in _PRICES_USD_PER_MTOK:
        return model_id
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
        model_id: Provider-specific or logical model identifier. Bedrock
            inference profiles, Vertex revisions, and direct names all
            resolve to the same Cortex logical tier.
        input_tokens: Non-cached prompt tokens billed at the input rate.
        output_tokens: Completion tokens billed at the output rate.
        cache_read: Tokens served from the ephemeral prompt cache. Bill
            at the input rate (Anthropic publishes a 10% discount but
            many billing reports still show the full rate; we err on the
            conservative side and bill the full input rate to avoid
            understating cost).
        cache_write: Tokens written to the ephemeral cache, billed at
            ``1.25x`` the input rate.

    Returns:
        Estimated USD cost, never negative.

    Raises:
        KeyError: when ``model_id`` is unknown.
    """
    if input_tokens < 0 or output_tokens < 0 or cache_read < 0 or cache_write < 0:
        raise ValueError(
            "Token counts must be non-negative; got "
            f"in={input_tokens} out={output_tokens} "
            f"cache_read={cache_read} cache_write={cache_write}",
        )
    logical = _logical_model(model_id)
    input_rate, output_rate = _PRICES_USD_PER_MTOK[logical]
    cost = (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_read * input_rate
        + cache_write * input_rate * _CACHE_WRITE_MULTIPLIER
    ) / 1_000_000.0
    return float(cost)


__all__ = ["usd_cost"]
