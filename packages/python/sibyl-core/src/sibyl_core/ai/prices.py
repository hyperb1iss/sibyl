"""Prices for models newer than the bundled genai-prices data.

genai-prices 0.1.8 has no Claude Haiku 5.5 or GPT-6.1 Sol entry, so pricing
one of their responses raises ``LookupError`` and the call is recorded at no
cost. It prices Claude Sonnet 5.5 through its ``claude-sonnet-5`` prefix
match, which charges cache reads at Sonnet 5's $0.20 instead of $0.10. The
entries here go ahead of the bundled ones for the first-party Anthropic,
OpenAI and Bedrock providers, so they win for these models and every other
model prices exactly as before.

An entry can go once a genai-prices release carries the same rates.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from functools import cache

from genai_prices import data_snapshot, types
from genai_prices.types import (
    ClauseContains,
    ClauseEndsWith,
    ClauseEquals,
    ClauseOr,
    ClauseStartsWith,
    MatchLogic,
    ModelInfo,
    ModelPrice,
    PriceCalculation,
    Tier,
    TieredPrices,
)
from pydantic_ai.usage import RequestUsage

#: Bedrock inference-profile prefixes that route inside a geography, each
#: priced 10% above the global profile.
_BEDROCK_GEO_PREFIXES = ("us", "eu", "apac", "au", "jp", "us-gov")

#: Claude Haiku 5.5 prices every token of a request at its long-context rate
#: once the prompt (cache reads and writes included) exceeds 100,000 tokens.
#: genai-prices selects a tier when the input count is greater than its start.
_HAIKU_5_5_LONG_PROMPT_START = 100_000


def _usd(value: str) -> Decimal:
    return Decimal(value)


def _haiku_5_5_prices(scale: str) -> ModelPrice:
    factor = Decimal(scale)

    def tiered(base: str, long_prompt: str) -> TieredPrices:
        return TieredPrices(
            base=_usd(base) * factor,
            tiers=[Tier(start=_HAIKU_5_5_LONG_PROMPT_START, price=_usd(long_prompt) * factor)],
        )

    return ModelPrice(
        input_mtok=tiered("0.10", "0.50"),
        output_mtok=tiered("0.50", "2.50"),
        cache_read_mtok=tiered("0.01", "0.05"),
        cache_write_mtok=tiered("0.125", "0.625"),
        cache_write_1h_mtok=tiered("0.20", "1.00"),
    )


def _sonnet_5_5_prices(scale: str) -> ModelPrice:
    factor = Decimal(scale)
    return ModelPrice(
        input_mtok=_usd("2") * factor,
        output_mtok=_usd("10") * factor,
        cache_read_mtok=_usd("0.10") * factor,
        cache_write_mtok=_usd("2.50") * factor,
        cache_write_1h_mtok=_usd("4") * factor,
    )


#: OpenAI charges 2x input and cache and 1.5x output on the whole request for
#: prompts over 272K input tokens. genai-prices selects a tier when the count
#: is greater than the start, and writes its GPT-6 entries with 272,000.
_OPENAI_LONG_PROMPT_START = 272_000


def _gpt_6_1_sol_prices() -> ModelPrice:
    def tiered(base: str, multiplier: str) -> TieredPrices:
        return TieredPrices(
            base=_usd(base),
            tiers=[Tier(start=_OPENAI_LONG_PROMPT_START, price=_usd(base) * Decimal(multiplier))],
        )

    return ModelPrice(
        input_mtok=tiered("2", "2"),
        cache_read_mtok=tiered("0.10", "2"),
        cache_write_mtok=tiered("2.50", "2"),
        output_mtok=tiered("10", "1.5"),
    )


def _first_party_match(alias: str) -> MatchLogic:
    return ClauseOr(or_=[ClauseEquals(equals=alias), ClauseStartsWith(starts_with=f"{alias}-")])


def _bedrock_global_match(alias: str) -> MatchLogic:
    return ClauseOr(
        or_=[
            ClauseEndsWith(ends_with=f"global.anthropic.{alias}"),
            ClauseContains(contains=f"global.anthropic.{alias}-v"),
        ]
    )


def _bedrock_regional_match(alias: str) -> MatchLogic:
    return ClauseOr(
        or_=[
            ClauseEquals(equals=f"anthropic.{alias}"),
            ClauseEquals(equals=alias),
            ClauseStartsWith(starts_with=f"anthropic.{alias}-v"),
            *(ClauseEquals(equals=f"{geo}.anthropic.{alias}") for geo in _BEDROCK_GEO_PREFIXES),
            *(
                ClauseContains(contains=f"{geo}.anthropic.{alias}-v")
                for geo in _BEDROCK_GEO_PREFIXES
            ),
        ]
    )


_ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
_OPENAI_SOURCE = "https://developers.openai.com/api/docs/models/gpt-6.1-sol"
_BEDROCK_SOURCE = (
    "AWS price list API, AmazonBedrockFoundationModels, us-west-2 "
    "(https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrockFoundationModels/current/index.json)"
)


def _first_party_models() -> list[ModelInfo]:
    return [
        ModelInfo(
            id="claude-haiku-5-5",
            match=_first_party_match("claude-haiku-5-5"),
            name="Claude Haiku 5.5",
            context_window=1_000_000,
            price_comments=(
                f"Prompts over 100,000 tokens pay 5x on every token. Ref: {_ANTHROPIC_SOURCE}"
            ),
            prices=_haiku_5_5_prices("1"),
        ),
        ModelInfo(
            id="claude-sonnet-5-5",
            match=_first_party_match("claude-sonnet-5-5"),
            name="Claude Sonnet 5.5",
            context_window=1_000_000,
            price_comments=f"Cache hits are 0.05x base input. Ref: {_ANTHROPIC_SOURCE}",
            prices=_sonnet_5_5_prices("1"),
        ),
    ]


def _openai_models() -> list[ModelInfo]:
    return [
        ModelInfo(
            id="gpt-6.1-sol",
            match=_first_party_match("gpt-6.1-sol"),
            name="GPT-6.1 Sol",
            context_window=1_050_000,
            price_comments=f"Long-context rates from 272K input tokens. Ref: {_OPENAI_SOURCE}",
            prices=_gpt_6_1_sol_prices(),
        )
    ]


def _bedrock_models() -> list[ModelInfo]:
    models: list[ModelInfo] = []
    for alias, name, prices in (
        ("claude-haiku-5-5", "Claude Haiku 5.5", _haiku_5_5_prices),
        ("claude-sonnet-5-5", "Claude Sonnet 5.5", _sonnet_5_5_prices),
    ):
        models.append(
            ModelInfo(
                id=f"global.anthropic.{alias}",
                match=_bedrock_global_match(alias),
                name=name,
                context_window=1_000_000,
                price_comments=f"Global cross-Region inference. Ref: {_BEDROCK_SOURCE}",
                prices=prices("1"),
            )
        )
        models.append(
            ModelInfo(
                id=f"regional.anthropic.{alias}",
                match=_bedrock_regional_match(alias),
                name=name,
                context_window=1_000_000,
                price_comments=(
                    f"In-Region and geography profiles, 10% above global. Ref: {_BEDROCK_SOURCE}"
                ),
                prices=prices("1.1"),
            )
        )
    return models


@cache
def _supplement_ids() -> frozenset[str]:
    return frozenset(
        model.id for model in (*_first_party_models(), *_openai_models(), *_bedrock_models())
    )


@cache
def price_snapshot() -> data_snapshot.DataSnapshot:
    """genai-prices' bundled data with Sibyl's entries ahead of the bundled ones."""
    bundled = data_snapshot.get_snapshot()
    supplements = {
        "anthropic": _first_party_models(),
        "openai": _openai_models(),
        "aws": _bedrock_models(),
    }
    providers: list[types.Provider] = [
        replace(provider, models=[*supplements[provider.id], *provider.models])
        if provider.id in supplements
        else provider
        for provider in bundled.providers
    ]
    return data_snapshot.DataSnapshot(providers=providers, from_auto_update=False)


def calc_price(
    usage: RequestUsage,
    model_ref: str,
    *,
    provider_id: str | None = None,
    provider_api_url: str | None = None,
    genai_request_timestamp: datetime | None = None,
) -> PriceCalculation:
    """Price one response's usage, trying the API URL before the provider name.

    This is the order pydantic-ai's ``ModelResponse.cost()`` uses. Raises
    ``LookupError`` when neither identifies a priced model.
    """
    snapshot = price_snapshot()
    if provider_api_url:
        try:
            return snapshot.calc(usage, model_ref, None, provider_api_url, genai_request_timestamp)
        except LookupError:
            pass
    return snapshot.calc(usage, model_ref, provider_id, None, genai_request_timestamp)


def supplement_price(
    usage: RequestUsage,
    model_ref: str,
    *,
    provider_id: str | None = None,
    provider_api_url: str | None = None,
    genai_request_timestamp: datetime | None = None,
) -> PriceCalculation | None:
    """Sibyl's price for a model one of its entries covers, or ``None``.

    ``None`` leaves the response to genai-prices as bundled, so every model
    without an entry here prices exactly as it did before.
    """
    try:
        price = calc_price(
            usage,
            model_ref,
            provider_id=provider_id,
            provider_api_url=provider_api_url,
            genai_request_timestamp=genai_request_timestamp,
        )
    except LookupError:
        return None
    return price if price.model.id in _supplement_ids() else None
