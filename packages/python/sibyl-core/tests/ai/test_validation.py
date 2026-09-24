from __future__ import annotations

import pytest
from pydantic import SecretStr
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from sibyl_core.ai import validation
from sibyl_core.ai.llm.config import (
    ConfigField,
    LLMConfig,
    LLMSurface,
    ResolvedLLMConfig,
)


class StaticConfigSource:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig:
        return ResolvedLLMConfig(
            surface=surface,
            provider=ConfigField(value=self.config.provider, source="db"),
            model=ConfigField(value=self.config.model, source="db"),
            temperature=ConfigField(value=self.config.temperature, source="db"),
            max_tokens=ConfigField(value=self.config.max_tokens, source="db"),
            timeout_seconds=ConfigField(value=self.config.timeout_seconds, source="db"),
            api_key=ConfigField(value=self.config.api_key, source="db"),
        )

    async def invalidate(self, surface: LLMSurface | None = None) -> None:
        return None


@pytest.mark.asyncio
async def test_check_provider_key_returns_valid_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        validation,
        "build_model",
        lambda config: TestModel(custom_output_text="ok", model_name=config.model),
    )

    result = await validation.check_provider_key("openai", "key")

    assert result.valid is True
    assert result.status == "valid"
    assert result.model == "gpt-5.4-nano"
    assert result.output_tokens is not None


@pytest.mark.asyncio
async def test_check_model_availability_classifies_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(404, "missing-model", {"error": "missing"})

    monkeypatch.setattr(validation, "build_model", lambda _: FunctionModel(fail))

    result = await validation.check_model_availability("gemini", "missing-model", "key")

    assert result.valid is False
    assert result.status == "model_not_found"
    assert result.requested_model == "missing-model"


@pytest.mark.asyncio
async def test_surface_config_returns_parsed_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        validation,
        "build_model",
        lambda _: TestModel(custom_output_args={"ok": True, "summary": "ready"}),
    )
    source = StaticConfigSource(
        LLMConfig(
            provider="anthropic",
            model="claude-haiku-4-5",
            api_key=SecretStr("anthropic-key"),
        )
    )

    result = await validation.test_surface_config(LLMSurface.CRAWLER, source)

    assert result.valid is True
    assert result.status == "valid"
    assert result.parsed_output == {"ok": True, "summary": "ready"}
    assert result.model == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "native"), [("claude-opus-5-5", True), ("claude-opus-5", False)])
async def test_surface_probe_never_forces_a_tool_on_a_model_that_rejects_it(
    monkeypatch: pytest.MonkeyPatch, model: str, native: bool
) -> None:
    import json

    import httpx2 as httpx

    from sibyl_core.ai import providers
    from sibyl_core.ai.transport import RecordingAnthropicClient

    probe = {"ok": True, "summary": "ready"}
    wires: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wires.append(body)
        if native:
            content = [{"type": "text", "text": json.dumps(probe)}]
        else:
            tool = body["tools"][0]["name"]
            content = [{"type": "tool_use", "id": "toolu_probe", "name": tool, "input": probe}]
        return httpx.Response(
            200,
            json={
                "id": "msg_probe",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": content,
                "stop_reason": "end_turn" if native else "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    monkeypatch.setattr(
        providers,
        "RecordingAnthropicClient",
        lambda: RecordingAnthropicClient(transport=httpx.MockTransport(respond)),
    )
    source = StaticConfigSource(
        LLMConfig(provider="anthropic", model=model, api_key=SecretStr("anthropic-key"))
    )

    result = await validation.test_surface_config(LLMSurface.MEMORY, source)

    assert result.valid is True, result.error
    assert result.parsed_output == probe
    assert ("tool_choice" in wires[0]) is not native
    if native:
        assert wires[0]["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "invalid_key"),
        (403, "permission_denied"),
        (404, "model_not_found"),
        (429, "rate_limited"),
        (500, "network"),
    ],
)
def test_status_for_http_code(status_code: int, expected: str) -> None:
    assert validation._status_for_http_code(status_code) == expected
