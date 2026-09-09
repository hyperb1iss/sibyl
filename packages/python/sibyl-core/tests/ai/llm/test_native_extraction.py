"""Native extraction preserves validation and explicit endpoint policy."""

import json
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from sibyl_core.ai.errors import LLMValidationError
from sibyl_core.ai.llm import Extractor
from sibyl_core.ai.llm import extractor as extraction
from sibyl_core.ai.transport import RecordingOpenAIClient
from sibyl_core.tasks.procedure_evidence import EvidenceProposal


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("output_kind", ["procedure", "abstention"])
async def test_native_extraction_wire_feedback_and_routing(monkeypatch, recover, output_kind):
    reserve = AsyncMock()
    monkeypatch.setattr(extraction, "reserve_llm_budget", reserve)
    requests = []

    def respond(request):
        wire = json.loads(request.content)
        requests.append(wire)
        content = {"procedure": '{"goal":{}}', "abstention_reason": None}
        if recover and len(requests) > 1:
            content = {"procedure": None, "abstention_reason": "Insufficient evidence"}
            if output_kind == "procedure":
                assertion = {
                    "statement": "Synthetic observation",
                    "label": "observed",
                    "support": [{"evidence_id": "synthetic"}],
                }
                content = {
                    "procedure": {
                        "goal": assertion,
                        "environment": [assertion],
                        "preconditions": [assertion],
                        "required_tools": [],
                        "actions": [
                            {"order": 1, "action": assertion, "success_criteria": assertion}
                        ],
                        "expected_result": assertion,
                        "failure_modes": [assertion],
                        "abstain_when": [assertion],
                    },
                    "abstention_reason": None,
                }
        return httpx2.Response(
            200,
            json={
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 0,
                "model": "qwen/qwen3-coder-next",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": f"msg_{len(requests)}",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": json.dumps(content), "annotations": []}
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            },
        )

    async with RecordingOpenAIClient(transport=httpx2.MockTransport(respond)) as http:
        client = AsyncOpenAI(
            api_key="fixture",
            base_url="https://openrouter.ai/api/v1",
            http_client=http,
            max_retries=2,
        )
        model = OpenAIResponsesModel(
            "qwen/qwen3-coder-next", provider=OpenAIProvider(openai_client=client)
        )
        agent = Agent(
            model, output_type=NativeOutput(EvidenceProposal, strict=True), retries={"output": 2}
        )
        extractor = Extractor(
            EvidenceProposal,
            agent=agent,
            output_mode="native_strict",
            openrouter_provider="parasail/bf16",
            max_tokens=2048,
        )
        if recover:
            result = await extractor.extract_with_usage("Synthetic contrast")
            assert (result.output.procedure is not None) == (output_kind == "procedure")
            assert (result.output.abstention_reason is not None) == (output_kind == "abstention")
            assert result.usage.input_tokens == 20
            assert len(result.usage.transport_attempts) == 2
        else:
            with pytest.raises(LLMValidationError) as error:
                await extractor.extract_with_usage("Synthetic contrast")
            assert len(error.value.details["extraction_usage"]["transport_attempts"]) == 3
    assert reserve.await_args.kwargs["attempt_envelope"] == 9
    schema = extraction.extraction_schema(EvidenceProposal, "native_strict")
    for request in requests:
        assert request["text"]["format"]["schema"] == schema
        assert request["text"]["format"]["strict"] is True
        assert request.get("tools", []) == []
        assert request["provider"] == {
            "only": ["parasail/bf16"],
            "require_parameters": True,
            "allow_fallbacks": False,
        }
    assert "model_type" in json.dumps(requests[1]["input"])


def test_native_extraction_schema_keeps_nullable_abstention():
    declared = extraction.extraction_schema(EvidenceProposal)
    strict = extraction.extraction_schema(EvidenceProposal, "native_strict")
    assert not declared.get("required")
    assert set(strict["required"]) == {"procedure", "abstention_reason"}
    assert strict["properties"]["procedure"]["anyOf"][1] == {"type": "null"}
    assert strict["properties"]["abstention_reason"]["anyOf"][1] == {"type": "null"}


async def test_native_extraction_agent_cache_separates_output_modes(monkeypatch):
    from sibyl_core.ai import clients
    from sibyl_core.ai.llm.config import EnvConfigSource

    monkeypatch.setattr(
        clients, "resolve_llm_config", EnvConfigSource({"SIBYL_LLM_PROVIDER": "openai"}).resolve
    )
    clients.invalidate_agent_cache()
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(400))
    ) as http:
        client = AsyncOpenAI(api_key="fixture", http_client=http)
        model = OpenAIResponsesModel(
            "qwen/qwen3-coder-next", provider=OpenAIProvider(openai_client=client)
        )
        monkeypatch.setattr(clients, "build_model", lambda _: model)
        tool = await Extractor(EvidenceProposal)._get_agent()
        native = await Extractor(EvidenceProposal, output_mode="native_strict")._get_agent()
        native_again = await Extractor(EvidenceProposal, output_mode="native_strict")._get_agent()
        assert native is native_again and native is not tool
    clients.invalidate_agent_cache()
