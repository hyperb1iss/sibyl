"""Structured LLM extraction helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelSettings

from sibyl_core.ai.clients import get_agent
from sibyl_core.ai.errors import LLMError, classify_llm_exception
from sibyl_core.ai.llm.budget import reserve_llm_budget
from sibyl_core.ai.llm.config import LLMSurface
from sibyl_core.observability import elapsed_ms, telemetry_registry


class ExtractionUsage(BaseModel):
    provider: str | None = None
    model: str | None = None
    requests: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None = None
    cost_complete: bool = False


@dataclass(frozen=True)
class ExtractionResult[T]:
    output: T
    usage: ExtractionUsage


class Extractor[T]:
    def __init__(
        self,
        output_type: type[T] | Any,
        *,
        surface: LLMSurface = LLMSurface.DEFAULT,
        system_prompt: str | Sequence[str] | None = None,
        model_override: str | None = None,
        output_retries: int | None = 2,
        max_tokens: int | None = None,
        agent: Agent[Any, Any] | None = None,
    ) -> None:
        self.output_type = output_type
        self.surface = surface
        self.system_prompt = system_prompt
        self.model_override = model_override
        self.output_retries = output_retries
        self.max_tokens = max_tokens
        self._agent = agent

    async def extract(self, prompt: str) -> T:
        return (await self.extract_with_usage(prompt)).output

    async def extract_with_usage(self, prompt: str) -> ExtractionResult[T]:
        started_at = time.perf_counter()
        try:
            await reserve_llm_budget(
                surface=self.surface.value,
                prompt=prompt,
                output_token_limit=self.max_tokens,
            )
            agent = await self._get_agent()
            result = await agent.run(
                prompt,
                model_settings=_model_settings(self.max_tokens),
            )
            telemetry_registry().record_llm_call(
                surface=self.surface.value,
                provider="runtime",
                model=self.model_override or "default",
                status="ok",
                duration_ms=elapsed_ms(started_at),
            )
            return ExtractionResult(
                output=result.output,
                usage=_extraction_usage(result),
            )
        except Exception as exc:
            telemetry_registry().record_llm_call(
                surface=self.surface.value,
                provider="runtime",
                model=self.model_override or "default",
                status="error",
                duration_ms=elapsed_ms(started_at),
            )
            raise self._classify(exc) from exc

    async def extract_many(
        self,
        prompts: Sequence[str],
        *,
        max_concurrent: int = 5,
    ) -> list[T | LLMError]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def run_one(prompt: str) -> T | LLMError:
            async with semaphore:
                try:
                    return await self.extract(prompt)
                except LLMError as exc:
                    return exc

        return await asyncio.gather(*(run_one(prompt) for prompt in prompts))

    async def _get_agent(self) -> Agent[Any, Any]:
        if self._agent is not None:
            return self._agent
        return await get_agent(
            self.surface,
            output_type=self.output_type,
            system_prompt=self.system_prompt,
            model_override=self.model_override,
            output_retries=self.output_retries,
        )

    def _classify(self, exc: Exception) -> LLMError:
        return classify_llm_exception(
            exc,
            model=self.model_override,
            surface=self.surface.value,
        )


def _model_settings(max_tokens: int | None) -> ModelSettings | None:
    if max_tokens is None:
        return None
    return ModelSettings(max_tokens=max_tokens)


def _extraction_usage(result: Any) -> ExtractionUsage:
    run_usage = result.usage
    responses = [message for message in result.new_messages() if isinstance(message, ModelResponse)]
    provider = next(
        (response.provider_name for response in reversed(responses) if response.provider_name),
        None,
    )
    model = next(
        (response.model_name for response in reversed(responses) if response.model_name),
        None,
    )
    total_cost = 0.0
    priced_responses = 0
    for response in responses:
        try:
            total_cost += float(response.cost().total_price)
        except (AssertionError, LookupError):
            continue
        priced_responses += 1
    cost_complete = bool(responses) and priced_responses == len(responses)
    return ExtractionUsage(
        provider=provider,
        model=model,
        requests=run_usage.requests,
        input_tokens=run_usage.input_tokens,
        output_tokens=run_usage.output_tokens,
        total_tokens=run_usage.total_tokens,
        cost_usd=total_cost if cost_complete else None,
        cost_complete=cost_complete,
    )
