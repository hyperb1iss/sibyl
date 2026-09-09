from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from pydantic import BaseModel, SecretStr
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse

from sibyl_core.ai import providers
from sibyl_core.ai.clients import _config_fingerprint
from sibyl_core.ai.errors import LLMError
from sibyl_core.ai.llm.budget import llm_budget_context, set_budget_enforcer
from sibyl_core.ai.llm.config import EnvConfigSource, LLMConfig, LLMSurface
from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.ai.transport import RecordingOpenAIClient
from sibyl_core.services import eval_publication


def response() -> dict:
    return {
        "id": "resp_mock",
        "object": "response",
        "created_at": 0,
        "model": "qwen/qwen3-coder-next",
        "status": "completed",
        "output": [
            {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
    }


@pytest.mark.parametrize("failure", [None, "http", "timeout", "invalid_json", "missing_usage"])
async def test_transport_attempts_preserve_unknown_usage(monkeypatch, failure):
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private exception body", request=request)
        if len(calls) < 3 or failure == "http":
            return httpx.Response(
                500,
                headers={"retry-after-ms": "1", "x-request-id": f"request_{len(calls)}"},
                json={"error": {"message": "private provider body"}},
            )
        if failure == "invalid_json":
            return httpx.Response(200, content=b"not json")
        payload = response()
        if failure == "missing_usage":
            payload.pop("usage")
        return httpx.Response(200, headers={"x-request-id": "request_3"}, json=payload)

    async with RecordingOpenAIClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(providers, "RecordingOpenAIClient", lambda: http)
        model = providers.build_model(
            LLMConfig(
                provider="openai", model="qwen/qwen3-coder-next", api_key=SecretStr("private-key")
            )
        )
        assert model.client.max_retries == 2
        extractor = Extractor(str, agent=Agent(model, retries={"output": 0}), output_retries=0)
        monkeypatch.setattr(
            ModelResponse, "cost", lambda _: SimpleNamespace(total_price=Decimal("0.1"))
        )
        if failure in {"http", "timeout", "invalid_json"}:
            with pytest.raises(LLMError) as caught:
                await extractor.extract_with_usage("private prompt")
            receipt = caught.value.details["extraction_usage"]
            assert receipt["usage_complete"] is False
        else:
            result = await extractor.extract_with_usage("private prompt")
            assert result.usage.requests == 1
            assert result.usage.input_tokens == (0 if failure == "missing_usage" else 10)
            receipt = result.usage.model_dump()
            assert receipt["transport_usage_complete"] is False
        assert len(calls) == 3
        assert len(receipt["transport_attempts"]) == 3
        assert not receipt["cost_complete"]
        assert receipt["cost_usd"] is None
        assert not receipt["transport_attempts"][0]["usage_known"]
        serialized = json.dumps(receipt)
        assert "private" not in serialized and "authorization" not in serialized.lower()


async def test_transport_receipts_are_isolated_for_concurrent_extractions(monkeypatch):
    async def respond(request):
        await asyncio.sleep(0)
        prompt = json.loads(request.content)["input"][0]["content"]
        return httpx.Response(200, headers={"x-request-id": prompt}, json=response())

    async with RecordingOpenAIClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(providers, "RecordingOpenAIClient", lambda: http)
        model = providers.build_model(
            LLMConfig(
                provider="openai", model="qwen/qwen3-coder-next", api_key=SecretStr("private-key")
            )
        )
        extractor = Extractor(str, agent=Agent(model), output_retries=0)
        results = await asyncio.gather(
            *(extractor.extract_with_usage(f"request_{i}") for i in range(8))
        )
        for i, result in enumerate(results):
            assert len(result.usage.transport_attempts) == 1
            assert result.usage.transport_attempts[0].request_id == f"request_{i}"
            assert result.usage.transport_usage_complete is True


async def test_transport_budget_includes_system_schema_and_retry_envelope(monkeypatch):
    class Payload(BaseModel):
        name: str

    reservations = []

    class Budget:
        async def reserve(self, context, *, surface, estimated_tokens):
            reservations.append(estimated_tokens)
            raise LLMError("stop before provider")

    def no_request(request):
        pytest.fail("budget must precede transport")

    async with RecordingOpenAIClient(transport=httpx.MockTransport(no_request)) as http:
        monkeypatch.setattr(providers, "RecordingOpenAIClient", lambda: http)
        model = providers.build_model(
            LLMConfig(
                provider="openai", model="qwen/qwen3-coder-next", api_key=SecretStr("private-key")
            )
        )
        extractor = Extractor(
            Payload,
            agent=Agent(model, output_type=Payload),
            system_prompt="system",
            output_retries=0,
            max_tokens=7,
        )
        set_budget_enforcer(Budget())
        try:
            with llm_budget_context(user_id="user", organization_id="org"), pytest.raises(LLMError):
                await extractor.extract("prompt")
        finally:
            set_budget_enforcer(None)
        expected = (
            len("system\nprompt\n" + json.dumps(Payload.model_json_schema(), sort_keys=True)) // 4
            + 7
        ) * 3
        assert reservations == [expected]


async def test_transport_policy_binds_config_cache_and_dependency_versions(monkeypatch):
    environment = {"SIBYL_LLM_MEMORY_PROVIDER": "openai", "SIBYL_LLM_MEMORY_MODEL": "test"}
    source = EnvConfigSource(environment)
    monkeypatch.setattr(eval_publication, "resolve_llm_config", source.resolve)
    original = await eval_publication._extractor_policy()
    config = (await source.resolve(LLMSurface.MEMORY)).to_llm_config()
    assert config.transport_max_retries == 2
    environment["SIBYL_LLM_MEMORY_TRANSPORT_MAX_RETRIES"] = "4"
    assert await eval_publication._extractor_policy() != original
    changed = (await source.resolve(LLMSurface.MEMORY)).to_llm_config()
    assert _config_fingerprint(changed) != _config_fingerprint(config)
    environment.pop("SIBYL_LLM_MEMORY_TRANSPORT_MAX_RETRIES")
    from sibyl_core.ai import transport

    monkeypatch.setattr(transport, "version", lambda _: "changed-version")
    assert await eval_publication._extractor_policy() != original
    environment["SIBYL_LLM_MEMORY_TRANSPORT_MAX_RETRIES"] = "-1"
    with pytest.raises(LLMError):
        await source.resolve(LLMSurface.MEMORY)


async def test_transport_cancellation_records_ambiguous_attempt_and_reraises(monkeypatch):
    dispatched = asyncio.Event()

    async def respond(request):
        dispatched.set()
        await asyncio.Event().wait()

    async with RecordingOpenAIClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(providers, "RecordingOpenAIClient", lambda: http)
        model = providers.build_model(
            LLMConfig(
                provider="openai", model="qwen/qwen3-coder-next", api_key=SecretStr("private-key")
            )
        )
        extractor = Extractor(str, agent=Agent(model), output_retries=0)
        task = asyncio.create_task(extractor.extract_with_usage("private prompt"))
        await dispatched.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        usage = caught.value.extraction_usage
        assert len(usage["transport_attempts"]) == 1
        assert usage["transport_attempts"][0]["exception_type"] == "CancelledError"
        assert usage["usage_complete"] is False
        assert usage["cost_usd"] is None


async def test_transport_collection_child_sharing_and_nested_isolation():
    from sibyl_core.ai.transport import collect_transport_attempts

    async with RecordingOpenAIClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as http:
        with collect_transport_attempts() as parent:
            await asyncio.create_task(http.get("https://example.invalid"))
            assert len(parent) == 1
            with collect_transport_attempts() as nested:
                await asyncio.create_task(http.get("https://example.invalid"))
                assert len(nested) == 1
                assert len(parent) == 1
            await http.get("https://example.invalid")
            assert len(parent) == 2


async def test_default_extractor_budget_uses_known_agent_output_settings():
    from pydantic_ai.models.test import TestModel

    reservations = []

    class Budget:
        async def reserve(self, context, *, surface, estimated_tokens):
            reservations.append(estimated_tokens)

    extractor = Extractor(
        str,
        agent=Agent(TestModel(settings={"max_tokens": 17}), model_settings={"max_tokens": 23}),
        output_retries=None,
    )
    set_budget_enforcer(Budget())
    try:
        with llm_budget_context(user_id="user", organization_id="org"):
            await extractor.extract("abcd")
    finally:
        set_budget_enforcer(None)
    # No explicit output retry policy: this remains a one-request estimate.
    assert reservations == [
        len("abcd\n" + json.dumps({"type": "string"}, sort_keys=True)) // 4 + 23
    ]
