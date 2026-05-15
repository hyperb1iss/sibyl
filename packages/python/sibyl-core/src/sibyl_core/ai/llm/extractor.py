"""Structured LLM extraction helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from pydantic_ai import Agent

from sibyl_core.ai.clients import get_agent
from sibyl_core.ai.errors import LLMError, classify_llm_exception
from sibyl_core.ai.llm.config import LLMSurface


class Extractor[T]:
    def __init__(
        self,
        output_type: type[T] | Any,
        *,
        surface: LLMSurface = LLMSurface.DEFAULT,
        system_prompt: str | Sequence[str] | None = None,
        model_override: str | None = None,
        output_retries: int | None = 2,
        agent: Agent[Any, Any] | None = None,
    ) -> None:
        self.output_type = output_type
        self.surface = surface
        self.system_prompt = system_prompt
        self.model_override = model_override
        self.output_retries = output_retries
        self._agent = agent

    async def extract(self, prompt: str) -> T:
        try:
            agent = await self._get_agent()
            result = await agent.run(prompt)
            return result.output
        except Exception as exc:
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
