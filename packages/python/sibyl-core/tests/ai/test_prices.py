"""The genai-prices rates extraction cost accounting charges for newer models.

Sibyl carried its own entries for these models until genai-prices 0.1.13,
the floor pydantic-ai 2.60 requires, priced them identically.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from genai_prices import calc_price
from pydantic_ai.usage import RequestUsage


def total(model_ref: str, usage: RequestUsage, **kwargs: str) -> Decimal:
    return calc_price(usage, model_ref, **kwargs).total_price


SHORT = RequestUsage(input_tokens=100_000, output_tokens=1_000_000)
LONG = RequestUsage(input_tokens=100_001, output_tokens=1_000_000)


def test_haiku_5_5_charges_every_token_at_the_long_rate_past_100k_prompt_tokens() -> None:
    assert total("claude-haiku-5-5", SHORT, provider_id="anthropic") == Decimal("0.51")
    assert total("claude-haiku-5-5", LONG, provider_id="anthropic") == Decimal("2.5500005")


def test_haiku_5_5_cache_tokens_count_toward_the_long_prompt_threshold() -> None:
    cached = RequestUsage(input_tokens=150_000, cache_read_tokens=140_000, output_tokens=0)
    # 10K uncached input at $0.50 and 140K cache reads at $0.05, both long-rate.
    assert total("claude-haiku-5-5", cached, provider_id="anthropic") == (
        Decimal("0.005") + Decimal("0.007")
    )


def test_sonnet_5_5_cache_reads_cost_half_of_sonnet_5s() -> None:
    cached = RequestUsage(input_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert total("claude-sonnet-5-5", cached, provider_id="anthropic") == Decimal("0.10")
    assert total("claude-sonnet-5", cached, provider_id="anthropic") == Decimal("0.2")


@pytest.mark.parametrize(
    ("model_ref", "expected"),
    [
        ("global.anthropic.claude-haiku-5-5", Decimal("0.51")),
        ("us.anthropic.claude-haiku-5-5", Decimal("0.561")),
        ("anthropic.claude-haiku-5-5", Decimal("0.561")),
        ("global.anthropic.claude-sonnet-5-5", Decimal("10.2")),
        ("us.anthropic.claude-sonnet-5-5", Decimal("11.22")),
        ("eu.anthropic.claude-sonnet-5-5", Decimal("11.22")),
    ],
)
def test_bedrock_geography_profiles_pay_ten_percent_over_global(
    model_ref: str, expected: Decimal
) -> None:
    assert total(model_ref, SHORT, provider_id="aws") == expected


def test_gpt_6_1_sol_prices_its_long_context_tier_over_272k_input_tokens() -> None:
    at = RequestUsage(input_tokens=272_000, output_tokens=1_000_000)
    over = RequestUsage(input_tokens=272_001, cache_read_tokens=72_001, output_tokens=1_000_000)
    # 272,000 x $2 + 1M x $10.
    assert total("gpt-6.1-sol", at, provider_id="openai") == Decimal("10.544")
    # 200K x $4 + 72,001 cache reads x $0.20 + 1M x $15.
    assert total("gpt-6.1-sol", over, provider_api_url="https://api.openai.com/v1") == (
        Decimal("0.8") + Decimal("0.0144002") + Decimal("15")
    )
