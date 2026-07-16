from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from sibyl_core.ai.errors import LLMProviderError, LLMRateLimitError, LLMValidationError
from sibyl_core.ai.llm import Extractor
from sibyl_core.ai.llm.budget import LLMBudgetContext, llm_budget_context, set_budget_enforcer


class Payload(BaseModel):
    name: str
    score: float


class RecordingBudgetEnforcer:
    def __init__(self) -> None:
        self.calls: list[tuple[LLMBudgetContext, str, int]] = []

    async def reserve(
        self,
        context: LLMBudgetContext,
        *,
        surface: str,
        estimated_tokens: int,
    ) -> None:
        self.calls.append((context, surface, estimated_tokens))


@pytest.fixture(autouse=True)
def reset_budget_enforcer() -> None:
    set_budget_enforcer(None)


@pytest.mark.asyncio
async def test_extractor_returns_parsed_model() -> None:
    agent = Agent(
        TestModel(custom_output_args={"name": "Sibyl", "score": 0.9}), output_type=Payload
    )
    extractor = Extractor(Payload, agent=agent)

    result = await extractor.extract("extract")

    assert result == Payload(name="Sibyl", score=0.9)


@pytest.mark.asyncio
async def test_extractor_returns_provider_usage_and_complete_cost() -> None:
    async def respond(_: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, {"name": "Sibyl", "score": 0.9})],
            usage=RequestUsage(input_tokens=12, output_tokens=4),
            model_name="gpt-test",
            provider_name="openai",
        )

    extractor = Extractor(Payload, agent=Agent(FunctionModel(respond), output_type=Payload))
    price = SimpleNamespace(total_price=Decimal("0.0012"))

    with patch.object(ModelResponse, "cost", return_value=price):
        result = await extractor.extract_with_usage("extract")

    assert result.output == Payload(name="Sibyl", score=0.9)
    assert result.usage.model_dump() == {
        "provider": "openai",
        "model": "function:respond:",
        "requests": 1,
        "input_tokens": 12,
        "output_tokens": 4,
        "total_tokens": 16,
        "cost_usd": 0.0012,
        "cost_complete": True,
    }


@pytest.mark.asyncio
async def test_extractor_maps_validation_failure() -> None:
    agent = Agent(FunctionModel(_invalid_json_response), output_type=Payload, retries={"output": 0})
    extractor = Extractor(Payload, agent=agent, output_retries=0)

    with pytest.raises(LLMValidationError):
        await extractor.extract("extract")


@pytest.mark.asyncio
async def test_extractor_maps_rate_limit_failure() -> None:
    async def fail(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(429, "test-model", {"error": "slow down"})

    extractor = Extractor(Payload, agent=Agent(FunctionModel(fail), output_type=Payload))

    with pytest.raises(LLMRateLimitError):
        await extractor.extract("extract")


@pytest.mark.asyncio
async def test_extract_many_returns_partial_errors() -> None:
    extractor = Extractor(
        Payload, agent=Agent(FunctionModel(_prompt_sensitive_response), output_type=Payload)
    )

    results = await extractor.extract_many(["good", "bad"], max_concurrent=2)

    assert results[0] == Payload(name="good", score=1.0)
    assert isinstance(results[1], LLMProviderError)


@pytest.mark.asyncio
async def test_extractor_applies_max_tokens_model_settings() -> None:
    captured_settings: list[ModelSettings | None] = []

    async def record_settings(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured_settings.append(info.model_settings)
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, {"name": "Sibyl", "score": 0.9})],
            model_name="function",
        )

    extractor = Extractor(
        Payload,
        agent=Agent(FunctionModel(record_settings), output_type=Payload),
        max_tokens=123,
    )

    result = await extractor.extract("extract")

    assert result == Payload(name="Sibyl", score=0.9)
    assert len(captured_settings) == 1
    assert captured_settings[0] is not None
    assert captured_settings[0].get("max_tokens") == 123


@pytest.mark.asyncio
async def test_extractor_reserves_budget_before_provider_call() -> None:
    enforcer = RecordingBudgetEnforcer()
    set_budget_enforcer(enforcer)
    agent = Agent(
        TestModel(custom_output_args={"name": "Sibyl", "score": 0.9}), output_type=Payload
    )
    extractor = Extractor(Payload, agent=agent, max_tokens=10)

    with llm_budget_context(user_id="user-1", organization_id="org-1"):
        await extractor.extract("abcd")

    assert len(enforcer.calls) == 1
    context, surface, tokens = enforcer.calls[0]
    assert context.user_id == "user-1"
    assert surface == "default"
    assert tokens == 11


async def _invalid_json_response(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("not json")], model_name="function")


async def _prompt_sensitive_response(
    messages: list[ModelMessage],
    info: AgentInfo,
) -> ModelResponse:
    prompt = _last_user_prompt(messages)
    if "bad" in prompt:
        raise ModelHTTPError(500, "test-model", {"error": "boom"})

    output_tool = info.output_tools[0]
    return ModelResponse(
        parts=[ToolCallPart(output_tool.name, {"name": prompt, "score": 1.0})],
        model_name="function",
    )


def _last_user_prompt(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                return content
    return ""
