from __future__ import annotations

from decimal import Decimal

import pytest
from genai_prices import data_snapshot
from pydantic_ai.usage import RequestUsage

from sibyl_core.ai.prices import calc_price, supplement_price


def total(model_ref: str, usage: RequestUsage, **kwargs: str) -> Decimal:
    return calc_price(usage, model_ref, **kwargs).total_price


SHORT = RequestUsage(input_tokens=100_000, output_tokens=1_000_000)
LONG = RequestUsage(input_tokens=100_001, output_tokens=1_000_000)


def test_haiku_5_5_charges_every_token_at_the_long_rate_past_100k_prompt_tokens() -> None:
    assert total("claude-haiku-5-5", SHORT, provider_id="anthropic") == Decimal("0.51")
    assert total("claude-haiku-5-5", LONG, provider_id="anthropic") == Decimal("2.5500005")


def test_haiku_5_5_cache_tokens_count_toward_the_long_prompt_threshold() -> None:
    cached = RequestUsage(input_tokens=150_000, cache_read_tokens=140_000, output_tokens=0)
    price = calc_price(cached, "claude-haiku-5-5", provider_id="anthropic")
    # 10K uncached input at $0.50 and 140K cache reads at $0.05, both long-rate.
    assert price.total_price == Decimal("0.005") + Decimal("0.007")


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


@pytest.mark.parametrize(
    ("model_ref", "provider_id", "expected"),
    [
        ("claude-opus-5-5", "anthropic", Decimal("20.4")),
        ("claude-haiku-4-5-20251001", "anthropic", Decimal("5.1")),
        ("claude-sonnet-5", "anthropic", Decimal("10.2")),
        ("us.anthropic.claude-opus-5-5", "aws", Decimal("22.44")),
        ("us.anthropic.claude-sonnet-5", "aws", Decimal("11.22")),
        ("gpt-5.4-mini", "openai", Decimal("4.575")),
    ],
)
def test_models_genai_prices_already_carries_price_as_before(
    model_ref: str, provider_id: str, expected: Decimal
) -> None:
    usage = SHORT
    assert total(model_ref, usage, provider_id=provider_id) == expected
    bundled = data_snapshot.get_snapshot().calc(usage, model_ref, provider_id, None, None)
    assert bundled.total_price == expected


def test_the_api_url_is_tried_before_the_provider_name() -> None:
    price = calc_price(
        SHORT,
        "claude-haiku-5-5",
        provider_id="not-a-provider",
        provider_api_url="https://api.anthropic.com",
    )
    assert price.model.id == "claude-haiku-5-5"


def test_genai_prices_global_snapshot_is_left_alone() -> None:
    calc_price(SHORT, "claude-haiku-5-5", provider_id="anthropic")
    assert data_snapshot._custom_snapshot is None
    with pytest.raises(LookupError):
        data_snapshot.get_snapshot().calc(SHORT, "claude-haiku-5-5", "anthropic", None, None)


def test_an_unknown_model_still_raises_lookup_error() -> None:
    with pytest.raises(LookupError):
        calc_price(SHORT, "claude-nonexistent-9", provider_id="anthropic")


def test_only_sibyls_own_entries_take_the_supplement_path() -> None:
    assert supplement_price(SHORT, "claude-haiku-5-5", provider_id="anthropic") is not None
    assert supplement_price(SHORT, "us.anthropic.claude-sonnet-5-5", provider_id="aws") is not None
    # Bundled models and unknown ones fall back to genai-prices' own path.
    assert supplement_price(SHORT, "claude-opus-5-5", provider_id="anthropic") is None
    assert supplement_price(SHORT, "us.anthropic.claude-sonnet-5", provider_id="aws") is None
    assert supplement_price(SHORT, "gpt-test", provider_id="openai") is None
    assert supplement_price(SHORT, "gpt-6.1-sol", provider_id="openai") is not None
    assert supplement_price(SHORT, "gpt-6-luna", provider_id="openai") is None


def test_gpt_6_1_sol_prices_its_long_context_tier_from_272k_input_tokens() -> None:
    below = RequestUsage(input_tokens=271_999, output_tokens=1_000_000)
    above = RequestUsage(input_tokens=272_000, cache_read_tokens=72_000, output_tokens=1_000_000)
    # 271,999 x $2 + 1M x $10.
    assert total("gpt-6.1-sol", below, provider_id="openai") == Decimal("10.543998")
    # 200K x $4 + 72K cache reads x $0.20 + 1M x $15.
    assert total("gpt-6.1-sol", above, provider_api_url="https://api.openai.com/v1") == (
        Decimal("0.8") + Decimal("0.0144") + Decimal("15")
    )
