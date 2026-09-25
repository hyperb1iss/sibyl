"""Structured LLM extraction helpers."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from weakref import WeakKeyDictionary

import structlog
from genai_prices import calc_price
from pydantic import BaseModel, Field, TypeAdapter
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model, ModelSettings
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.profiles.openai import OpenAIJsonSchemaTransformer

from sibyl_core.ai.bedrock import arn_model_id
from sibyl_core.ai.clients import get_agent
from sibyl_core.ai.errors import LLMError, classify_llm_exception
from sibyl_core.ai.llm.budget import (
    LLMReservation,
    collect_llm_reservations,
    estimate_llm_tokens,
    reserve_llm_budget,
    settle_llm_reservations,
)
from sibyl_core.ai.llm.config import LLMConfig, LLMSurface
from sibyl_core.ai.providers import prefers_native_output
from sibyl_core.ai.registry import canonical_model_alias, model_registry
from sibyl_core.ai.transport import (
    FailedExtractionUsage,
    RecordingAnthropicClient,
    RecordingOpenAIClient,
    TransportAttempt,
    collect_transport_attempts,
    reserve_uncovered_transport_attempts,
)
from sibyl_core.observability import elapsed_ms, telemetry_registry

log = structlog.get_logger()


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


OutputMode = Literal["tool", "native_strict"]


def effective_output_mode(output_mode: OutputMode, config: LLMConfig) -> OutputMode:
    """The structured-output mode a request can actually use on this model."""
    if output_mode == "tool" and prefers_native_output(config):
        return "native_strict"
    return output_mode


def extraction_schema(
    output_type: Any, output_mode: OutputMode = "tool", *, profile: ModelProfile | None = None
) -> dict[str, Any]:
    schema = TypeAdapter(output_type).json_schema()
    if output_mode == "native_strict":
        transformer = (profile or {}).get("json_schema_transformer") or OpenAIJsonSchemaTransformer
        return transformer(schema, strict=True).walk()
    return schema


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
        output_mode: OutputMode = "tool",
        openrouter_provider: str | None = None,
    ) -> None:
        if output_mode not in ("tool", "native_strict"):
            raise ValueError("unsupported extraction output mode")
        if openrouter_provider is not None and not openrouter_provider.strip():
            raise ValueError("provider endpoint must be nonempty")
        self.output_mode = output_mode
        self.openrouter_provider = openrouter_provider
        self.output_type = output_type
        self.surface = surface
        self.system_prompt = system_prompt
        self.model_override = model_override
        self.output_retries = output_retries
        self.max_tokens = max_tokens
        self._agent = agent
        self._prepared_agents: WeakKeyDictionary[asyncio.AbstractEventLoop, Agent[Any, Any]] = (
            WeakKeyDictionary()
        )

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
            mode = self._mode_of(agent)
            if mode == "native_strict" and not isinstance(
                agent.model, OpenAIResponsesModel | AnthropicModel
            ):
                raise ValueError("native strict extraction requires OpenAI Responses or Anthropic")
            if (
                mode == "native_strict"
                and isinstance(agent.model, AnthropicModel)
                and not agent.model.profile.get("supports_json_schema_output", False)
            ):
                raise ValueError("Anthropic model does not support native strict extraction")
            if self.openrouter_provider is not None and (
                not isinstance(agent.model, OpenAIResponsesModel)
                or agent.model.client.base_url.host != "openrouter.ai"
                or agent.model.client.base_url.scheme != "https"
                or agent.model.client.base_url.port not in (None, 443)
            ):
                raise ValueError("OpenRouter routing requires the OpenRouter API origin")
            # Dynamic agent settings and provider work are not bounded here.
            budget_prompt = self._budget_prompt(prompt, agent, mode)
            output_limit = self._budget_output_limit(agent)
            attempt_tokens = estimate_llm_tokens(budget_prompt, output_token_limit=output_limit)
            # A recording transport reserves every HTTP hop as it dispatches, an
            # SDK retry and an output retry alike, so one attempt up front
            # suffices. Any other model's retries never pass through that hook,
            # so its output retries are reserved up front as before.
            recorded = _records_transport(agent.model)
            up_front = 1 if recorded else (self.output_retries or 0) + 1

            async def reserve(envelope: int = 1) -> None:
                await reserve_llm_budget(
                    surface=self.surface.value,
                    prompt=budget_prompt,
                    output_token_limit=output_limit,
                    attempt_envelope=envelope,
                )

            with collect_llm_reservations() as reservations:
                await reserve(up_front)
                try:
                    with reserve_uncovered_transport_attempts(up_front, reserve):
                        result = await agent.run(
                            prompt,
                            model_settings=self._model_settings(),
                        )
                except BaseException:
                    # Usage is unknown after a failure. A recorded call keeps one
                    # attempt's estimate per dispatched hop, and hops that never
                    # left keep nothing; an unrecorded call keeps its reservation.
                    if recorded:
                        await self._settle(reservations, attempt_tokens * len(attempts))
                    raise
                usage = _extraction_usage(result, attempts, llm=agent.model)
                reserved_total = sum(item.tokens for item in reservations)
                await self._settle(
                    reservations,
                    usage.total_tokens if usage.total_tokens > 0 else reserved_total,
                )
            telemetry_registry().record_llm_call(
                surface=self.surface.value,
                provider="runtime",
                model=self.model_override or "default",
                status="ok",
                duration_ms=elapsed_ms(started_at),
            )
            return ExtractionResult(output=result.output, usage=usage)
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

    async def _settle(self, reservations: list[LLMReservation], actual_tokens: int) -> None:
        """Hand back the unused part of a reservation; a settle failure never fails the call."""
        if not reservations:
            return
        try:
            await settle_llm_reservations(
                surface=self.surface.value,
                reservations=reservations,
                actual_tokens=actual_tokens,
            )
        except Exception:
            log.warning(
                "llm_budget_settle_failed",
                surface=self.surface.value,
                reserved_tokens=sum(item.tokens for item in reservations),
                actual_tokens=actual_tokens,
                exc_info=True,
            )

    def _budget_output_limit(self, agent: Agent[Any, Any]) -> int | None:
        """Use known static settings; dynamic settings and absent limits stay unknown."""
        if self.max_tokens is not None:
            return self.max_tokens
        settings = dict(agent.model.settings or {}) if isinstance(agent.model, Model) else {}
        if isinstance(agent.model_settings, dict):
            settings.update(agent.model_settings)
        return settings.get("max_tokens")

    def _budget_prompt(self, prompt: str, agent: Agent[Any, Any], mode: OutputMode) -> str:
        instructions = self.system_prompt or ()
        if isinstance(instructions, str):
            instructions = (instructions,)
        schema = extraction_schema(
            self.output_type,
            mode,
            profile=agent.model.profile if isinstance(agent.model, Model) else None,
        )
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

    async def resolved_output_mode(self) -> OutputMode:
        """The mode of the agent this extraction runs on in the current loop."""
        return self._mode_of(await self._prepared_agent())

    async def resolved_effort(self) -> str | None:
        """The Anthropic effort the agent sends, if any."""
        agent = await self._prepared_agent()
        settings = agent.model.settings if isinstance(agent.model, Model) else None
        effort = (settings or {}).get("anthropic_effort")
        return effort if isinstance(effort, str) else None

    def _mode_of(self, agent: Agent[Any, Any]) -> OutputMode:
        # An agent supplied by the caller already fixed its output type, so its
        # declared mode stands; the caller chooses with ``effective_output_mode``.
        if agent is self._agent:
            return self.output_mode
        return "native_strict" if isinstance(agent.output_type, NativeOutput) else "tool"

    async def _prepared_agent(self) -> Agent[Any, Any]:
        """Pin one agent to this loop so mode, schema and run share a model."""
        try:
            agent = await self._get_agent()
        except LLMError:
            raise
        except Exception as exc:
            # Building the model can fail before any request, on a missing key
            # for one; callers get the same classified error extraction raises.
            raise self._classify(exc) from exc
        if agent is not self._agent:
            self._prepared_agents[asyncio.get_running_loop()] = agent
        return agent

    async def output_schema(self) -> dict[str, Any]:
        """Resolve the schema from the same model used for this extraction."""
        agent = await self._prepared_agent()
        mode = self._mode_of(agent)
        if mode == "tool":
            return extraction_schema(self.output_type)
        return extraction_schema(
            self.output_type,
            mode,
            profile=agent.model.profile if isinstance(agent.model, Model) else None,
        )

    async def _get_agent(self) -> Agent[Any, Any]:
        if self._agent is not None:
            return self._agent
        prepared = self._prepared_agents.get(asyncio.get_running_loop())
        if prepared is not None:
            return prepared
        output_type: Any = self.output_type
        return await get_agent(
            self.surface,
            output_type=(
                NativeOutput(output_type, strict=True)
                if self.output_mode == "native_strict"
                else self.output_type
            ),
            system_prompt=self.system_prompt,
            model_override=self.model_override,
            output_retries=self.output_retries,
        )

    def _model_settings(self) -> ModelSettings | None:
        settings = _model_settings(self.max_tokens)
        if self.openrouter_provider is not None:
            settings = settings or ModelSettings()
            settings["extra_body"] = {
                "provider": {
                    "only": [self.openrouter_provider],
                    "require_parameters": True,
                    "allow_fallbacks": False,
                }
            }
        return settings

    def _classify(self, exc: Exception) -> LLMError:
        return classify_llm_exception(
            exc,
            model=self.model_override,
            surface=self.surface.value,
        )


def _records_transport(model: object) -> bool:
    """True when every HTTP hop of this model passes through Sibyl's recording client."""
    if not isinstance(model, OpenAIResponsesModel | AnthropicModel):
        return False
    http = getattr(model.client, "_client", None)
    return isinstance(http, RecordingOpenAIClient | RecordingAnthropicClient)


def _model_settings(max_tokens: int | None) -> ModelSettings | None:
    if max_tokens is None:
        return None
    return ModelSettings(max_tokens=max_tokens)


def _bedrock_price_ref(model: object) -> str | None:
    """The wire model ID to price a Bedrock response by, or ``None`` elsewhere.

    pydantic-ai prices by API URL and response model name, and Bedrock echoes
    the bare Claude name, so every bedrock-runtime call would price at the
    in-Region rate. The routed ID tells ``global.`` from ``us.`` profiles.
    """
    if not isinstance(model, AnthropicModel):
        return None
    from anthropic import AsyncAnthropicBedrock, AsyncAnthropicBedrockMantle

    if isinstance(model.client, AsyncAnthropicBedrock):
        return arn_model_id(model.model_name) or model.model_name
    if isinstance(model.client, AsyncAnthropicBedrockMantle):
        # Mantle's in-Region IDs drop the version suffix the price table keys on.
        entry = model_registry.get(canonical_model_alias(model.model_name))
        return (entry.platform_model_ids.get("bedrock") if entry else None) or model.model_name
    return None


def _response_cost(response: ModelResponse, bedrock_ref: str | None) -> float:
    if bedrock_ref is None:
        return float(response.cost().total_price)
    return float(
        calc_price(
            response.usage,
            bedrock_ref,
            provider_id="aws",
            genai_request_timestamp=response.timestamp,
        ).total_price
    )


def _extraction_usage(
    result: Any, attempts: list[TransportAttempt], *, llm: object = None
) -> ExtractionUsage:
    run_usage = result.usage
    bedrock_ref = _bedrock_price_ref(llm)
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
            total_cost += _response_cost(response, bedrock_ref)
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
