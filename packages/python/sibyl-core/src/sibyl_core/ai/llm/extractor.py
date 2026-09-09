"""Structured LLM extraction helpers."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model, ModelSettings
from pydantic_ai.models.openai import OpenAIResponsesModel

from sibyl_core.ai.clients import get_agent
from sibyl_core.ai.errors import LLMError, classify_llm_exception
from sibyl_core.ai.llm.budget import reserve_llm_budget
from sibyl_core.ai.llm.config import LLMSurface
from sibyl_core.ai.transport import (
    FailedExtractionUsage,
    TransportAttempt,
    collect_transport_attempts,
)
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
    transport_attempts: list[TransportAttempt] = Field(default_factory=list)
    transport_usage_complete: bool | None = None


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
        with collect_transport_attempts() as attempts:
            return await self._extract_with_attempts(prompt, started_at, attempts)

    async def _extract_with_attempts(
        self, prompt: str, started_at: float, attempts: list[TransportAttempt]
    ) -> ExtractionResult[T]:
        try:
            agent = await self._get_agent()
            transport_retries = (
                agent.model.client.max_retries
                if isinstance(agent.model, OpenAIResponsesModel)
                else 0
            )
            # An unspecified output retry policy estimates one model request;
            # dynamic agent settings and provider work are not bounded here.
            await reserve_llm_budget(
                surface=self.surface.value,
                prompt=self._budget_prompt(prompt),
                output_token_limit=self._budget_output_limit(agent),
                attempt_envelope=(transport_retries + 1) * ((self.output_retries or 0) + 1),
            )
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
                usage=_extraction_usage(result, attempts),
            )
        except asyncio.CancelledError as exc:
            exc.__dict__["extraction_usage"] = FailedExtractionUsage(
                transport_attempts=attempts
            ).model_dump(mode="json")
            raise
        except Exception as exc:
            telemetry_registry().record_llm_call(
                surface=self.surface.value,
                provider="runtime",
                model=self.model_override or "default",
                status="error",
                duration_ms=elapsed_ms(started_at),
            )
            error = self._classify(exc)
            error.details["extraction_usage"] = FailedExtractionUsage(
                transport_attempts=attempts
            ).model_dump(mode="json")
            raise error from exc

    def _budget_output_limit(self, agent: Agent[Any, Any]) -> int | None:
        """Use known static settings; dynamic settings and absent limits stay unknown."""
        if self.max_tokens is not None:
            return self.max_tokens
        settings = dict(agent.model.settings or {}) if isinstance(agent.model, Model) else {}
        if isinstance(agent.model_settings, dict):
            settings.update(agent.model_settings)
        return settings.get("max_tokens")

    def _budget_prompt(self, prompt: str) -> str:
        instructions = self.system_prompt or ()
        if isinstance(instructions, str):
            instructions = (instructions,)
        schema = TypeAdapter(self.output_type).json_schema()
        return "\n".join((*instructions, prompt, json.dumps(schema, sort_keys=True)))

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


def _extraction_usage(result: Any, attempts: list[TransportAttempt]) -> ExtractionUsage:
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
    successful = [
        i
        for i, attempt in enumerate(attempts)
        if attempt.status_code is not None and 200 <= attempt.status_code < 300
    ]
    if len(successful) == len(responses) and all(
        response.usage.has_values() for response in responses
    ):
        for index in successful:
            attempts[index] = attempts[index].model_copy(update={"usage_known": True})
    transport_complete = all(attempt.usage_known for attempt in attempts) if attempts else None
    cost_complete = (
        bool(responses) and priced_responses == len(responses) and transport_complete is not False
    )
    return ExtractionUsage(
        provider=provider,
        model=model,
        requests=run_usage.requests,
        input_tokens=run_usage.input_tokens,
        output_tokens=run_usage.output_tokens,
        total_tokens=run_usage.total_tokens,
        cost_usd=total_cost if cost_complete else None,
        cost_complete=cost_complete,
        transport_attempts=attempts,
        transport_usage_complete=transport_complete,
    )
