"""Text generation helpers."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models import ModelSettings

from sibyl_core.ai.clients import get_agent
from sibyl_core.ai.errors import LLMError, classify_llm_exception
from sibyl_core.ai.llm.config import LLMSurface
from sibyl_core.observability import elapsed_ms, telemetry_registry


class Generator:
    def __init__(
        self,
        *,
        surface: LLMSurface = LLMSurface.DEFAULT,
        system_prompt: str | Sequence[str] | None = None,
        model_override: str | None = None,
        output_retries: int | None = 2,
        agent: Agent[Any, Any] | None = None,
    ) -> None:
        self.surface = surface
        self.system_prompt = system_prompt
        self.model_override = model_override
        self.output_retries = output_retries
        self._agent = agent

    async def generate(self, prompt: str, *, max_tokens: int | None = None) -> str:
        started_at = time.perf_counter()
        try:
            agent = await self._get_agent()
            result = await agent.run(
                prompt,
                output_type=str,
                model_settings=_model_settings(max_tokens),
            )
            telemetry_registry().record_llm_call(
                surface=self.surface.value,
                provider="runtime",
                model=self.model_override or "default",
                status="ok",
                duration_ms=elapsed_ms(started_at),
            )
            return result.output
        except Exception as exc:
            telemetry_registry().record_llm_call(
                surface=self.surface.value,
                provider="runtime",
                model=self.model_override or "default",
                status="error",
                duration_ms=elapsed_ms(started_at),
            )
            raise self._classify(exc) from exc

    async def stream(self, prompt: str, *, max_tokens: int | None = None) -> AsyncIterator[str]:
        started_at = time.perf_counter()
        try:
            agent = await self._get_agent()
            async with agent.run_stream(
                prompt,
                output_type=str,
                model_settings=_model_settings(max_tokens),
            ) as stream:
                async for text in stream.stream_text(delta=True):
                    yield text
            telemetry_registry().record_llm_call(
                surface=f"{self.surface.value}_stream",
                provider="runtime",
                model=self.model_override or "default",
                status="ok",
                duration_ms=elapsed_ms(started_at),
            )
        except Exception as exc:
            telemetry_registry().record_llm_call(
                surface=f"{self.surface.value}_stream",
                provider="runtime",
                model=self.model_override or "default",
                status="error",
                duration_ms=elapsed_ms(started_at),
            )
            raise self._classify(exc) from exc

    async def _get_agent(self) -> Agent[Any, Any]:
        if self._agent is not None:
            return self._agent
        return await get_agent(
            self.surface,
            output_type=str,
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
