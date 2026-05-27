"""Cached PydanticAI agent construction."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from typing import Any
from weakref import WeakKeyDictionary

from pydantic_ai import Agent
from pydantic_ai.output import PromptedOutput

from sibyl_core.ai.llm.config import LLMConfig, LLMSurface, resolve_llm_config
from sibyl_core.ai.providers import build_model

AgentOutputType = Any
AgentCacheKey = tuple[str, str, str, tuple[str, ...], int | None]

# Per-loop cache. The outer WeakKeyDictionary drops a loop's bucket when the
# loop is garbage-collected, which matters because CPython will happily reuse
# a closed loop's id() for the next event loop and we'd otherwise return the
# stale Agent bound to the dead loop's resources.
_agent_cache: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[AgentCacheKey, Agent[Any, Any]]
] = WeakKeyDictionary()


async def get_agent(
    surface: LLMSurface = LLMSurface.DEFAULT,
    *,
    output_type: AgentOutputType = str,
    system_prompt: str | Sequence[str] | None = None,
    model_override: str | None = None,
    output_retries: int | None = 2,
) -> Agent[Any, Any]:
    resolved = await resolve_llm_config(surface)
    config = resolved.to_llm_config()
    if model_override is not None:
        config = config.model_copy(update={"model": model_override})

    loop = asyncio.get_running_loop()
    normalized_prompt = _normalize_system_prompt(system_prompt)
    key: AgentCacheKey = (
        surface.value,
        _config_fingerprint(config),
        _output_type_key(output_type),
        normalized_prompt,
        output_retries,
    )

    loop_bucket = _agent_cache.setdefault(loop, {})
    if key not in loop_bucket:
        loop_bucket[key] = Agent(
            model=build_model(config),
            output_type=_provider_output_type(config, output_type),
            instructions=normalized_prompt,
            output_retries=output_retries,
        )
    return loop_bucket[key]


def invalidate_agent_cache(surface: LLMSurface | None = None) -> None:
    if surface is None:
        _agent_cache.clear()
        return

    for loop_bucket in _agent_cache.values():
        for key in list(loop_bucket):
            if key[0] == surface.value:
                del loop_bucket[key]


def agent_cache_size() -> int:
    return sum(len(bucket) for bucket in _agent_cache.values())


def _normalize_system_prompt(system_prompt: str | Sequence[str] | None) -> tuple[str, ...]:
    if system_prompt is None:
        return ()
    if isinstance(system_prompt, str):
        return (system_prompt,)
    return tuple(system_prompt)


def _output_type_key(output_type: AgentOutputType) -> str:
    module = getattr(output_type, "__module__", None)
    qualname = getattr(output_type, "__qualname__", None)
    if module and qualname:
        return f"{module}.{qualname}"
    return repr(output_type)


def _provider_output_type(config: LLMConfig, output_type: AgentOutputType) -> AgentOutputType:
    if config.provider == "gemini" and output_type is not str:
        return PromptedOutput(output_type)
    return output_type


def _config_fingerprint(config: LLMConfig) -> str:
    api_key_hash = None
    if config.api_key is not None:
        api_key_hash = hashlib.sha256(config.api_key.get_secret_value().encode()).hexdigest()

    payload = "|".join(
        [
            config.provider,
            config.model,
            str(config.temperature),
            str(config.max_tokens),
            str(config.timeout_seconds),
            api_key_hash or "",
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()
