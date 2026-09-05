"""Pricing table + provider-id normaliser (audit D3).

The old table priced Opus 4.7 at $15/$75 (real list price $5/$25) and
billed cache reads at the full input rate. These tests pin the first-party
list prices for all five logical tiers, the published cache multipliers,
and the normalisation of every provider id shape back to a logical id.
"""

from __future__ import annotations

from typing import get_args

import pytest

from cortex.libs.config.settings import LogicalModelId
from cortex.libs.llm.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    PRICES_USD_PER_MTOK,
    normalize_model_id,
    usd_cost,
)


@pytest.mark.parametrize(
    ("model", "input_rate", "output_rate"),
    [
        ("claude-opus-5", 5.0, 25.0),
        ("claude-sonnet-5", 2.0, 10.0),
        ("claude-haiku-4-5", 1.0, 5.0),
        ("claude-opus-4-7", 5.0, 25.0),
        ("claude-sonnet-4-6", 3.0, 15.0),
    ],
)
def test_first_party_list_prices(model: str, input_rate: float, output_rate: float) -> None:
    assert usd_cost(model, input_tokens=1_000_000, output_tokens=0) == pytest.approx(input_rate)
    assert usd_cost(model, input_tokens=0, output_tokens=1_000_000) == pytest.approx(output_rate)


def test_table_covers_exactly_the_logical_literal() -> None:
    assert set(PRICES_USD_PER_MTOK) == set(get_args(LogicalModelId))


def test_opus_4_7_no_longer_uses_the_legacy_15_75_rate() -> None:
    # D3 regression guard: the old table had (15.0, 75.0).
    assert PRICES_USD_PER_MTOK["claude-opus-4-7"] == (5.0, 25.0)


def test_cache_reads_bill_at_one_tenth_and_writes_at_1_25x() -> None:
    assert CACHE_READ_MULTIPLIER == pytest.approx(0.1)
    assert CACHE_WRITE_MULTIPLIER == pytest.approx(1.25)
    read = usd_cost("claude-sonnet-5", input_tokens=0, output_tokens=0, cache_read=1_000_000)
    assert read == pytest.approx(0.2)
    write = usd_cost("claude-sonnet-5", input_tokens=0, output_tokens=0, cache_write=1_000_000)
    assert write == pytest.approx(2.5)
    # Mixed call: 10k uncached + 90k cached reads + 2k output on Sonnet 5.
    mixed = usd_cost(
        "claude-sonnet-5", input_tokens=10_000, output_tokens=2_000, cache_read=90_000
    )
    assert mixed == pytest.approx((10_000 * 2.0 + 2_000 * 10.0 + 90_000 * 0.2) / 1e6)


@pytest.mark.parametrize(
    ("provider_id", "logical"),
    [
        ("claude-sonnet-5", "claude-sonnet-5"),
        ("anthropic.claude-sonnet-5", "claude-sonnet-5"),
        ("anthropic.claude-opus-5", "claude-opus-5"),
        ("anthropic.claude-haiku-4-5", "claude-haiku-4-5"),
        ("claude-haiku-4-5@20251001", "claude-haiku-4-5"),
        ("us.anthropic.claude-sonnet-4-6-v1:0", "claude-sonnet-4-6"),
        ("us.anthropic.claude-opus-4-7-v1:0", "claude-opus-4-7"),
        ("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("global.anthropic.claude-sonnet-5", "claude-sonnet-5"),
        ("claude-opus-4-7@20251101", "claude-opus-4-7"),
        ("  claude-opus-5  ", "claude-opus-5"),
    ],
)
def test_provider_ids_normalise_to_logical_ids(provider_id: str, logical: str) -> None:
    assert normalize_model_id(provider_id) == logical
    assert usd_cost(provider_id, input_tokens=1_000_000, output_tokens=0) == pytest.approx(
        PRICES_USD_PER_MTOK[logical][0]
    )


@pytest.mark.parametrize("bad", ["", "claude-3-opus", "gpt-5", "anthropic.claude-sonnet-9"])
def test_unknown_ids_raise_key_error(bad: str) -> None:
    with pytest.raises(KeyError):
        normalize_model_id(bad)
    with pytest.raises(KeyError):
        usd_cost(bad, input_tokens=1, output_tokens=1)


def test_negative_token_counts_are_rejected() -> None:
    with pytest.raises(ValueError):
        usd_cost("claude-sonnet-5", input_tokens=-1, output_tokens=0)
    with pytest.raises(ValueError):
        usd_cost("claude-sonnet-5", input_tokens=0, output_tokens=0, cache_read=-5)
