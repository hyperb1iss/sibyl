"""Consolidation keeps normal schema feedback separate from transport retries."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from sibyl_core.ai.errors import LLMValidationError
from sibyl_core.ai.llm import Extractor
from sibyl_core.ai.llm import extractor as extraction
from sibyl_core.ai.transport import RecordingOpenAIClient
from sibyl_core.tasks.consolidation import OUTPUT_RETRIES
from sibyl_core.tasks.procedure_evidence import EvidenceProposal


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("transport_retry", [False, True])
async def test_validation_feedback_retains_attempts(monkeypatch, recover, transport_retry):
    reserve = AsyncMock()
    monkeypatch.setattr(extraction, "reserve_llm_budget", reserve)
    monkeypatch.setattr(ModelResponse, "cost", lambda _: SimpleNamespace(total_price=0.01))
    requests = []
    responses = 0
    malformed = json.dumps({"procedure": '{"goal": "unterminated}'})

    def respond(request):
        nonlocal responses
        requests.append(json.loads(request.content))
        if transport_retry and len(requests) == 1:
            return httpx2.Response(
                500,
                json={"error": {"message": "temporary provider failure"}},
                headers={"retry-after-ms": "1"},
            )
        responses += 1
        arguments = (
            json.dumps({"abstention_reason": "Insufficient supported evidence"})
            if recover and responses > 1
            else malformed
        )
        return httpx2.Response(
            200,
            json={
                "id": f"resp_{responses}",
                "object": "response",
                "created_at": 0,
                "model": "qwen/qwen3-coder-next",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": f"fc_{responses}",
                        "call_id": f"call_{responses}",
                        "name": "final_result",
                        "arguments": arguments,
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            },
        )

    async with RecordingOpenAIClient(transport=httpx2.MockTransport(respond)) as http:
        client = AsyncOpenAI(
            api_key="fixture",
            base_url="https://example.invalid/v1",
            http_client=http,
            max_retries=2,
        )
        model = OpenAIResponsesModel(
            "qwen/qwen3-coder-next", provider=OpenAIProvider(openai_client=client)
        )
        agent = Agent(model, output_type=EvidenceProposal, retries={"output": OUTPUT_RETRIES})
        extractor = Extractor(
            EvidenceProposal, agent=agent, output_retries=OUTPUT_RETRIES, max_tokens=2048
        )
        if recover:
            result = await extractor.extract_with_usage("Contrast the supplied evidence")
            assert result.output.procedure is None
            assert result.output.abstention_reason == "Insufficient supported evidence"
            assert result.usage.requests == 2
            assert result.usage.input_tokens == 20
            assert result.usage.output_tokens == 4
            attempts = result.usage.transport_attempts
            assert all(attempt.usage_known for attempt in attempts[int(transport_retry) :])
            if transport_retry:
                assert not attempts[0].usage_known
                assert result.usage.cost_usd is None
                assert result.usage.transport_usage_complete is False
        else:
            with pytest.raises(LLMValidationError) as error:
                await extractor.extract_with_usage("Contrast the supplied evidence")
            failed = error.value.details["extraction_usage"]
            assert failed["cost_usd"] is None
            assert failed["usage_complete"] is False
            attempts = failed["transport_attempts"]
            assert all(not attempt["usage_known"] for attempt in attempts)

    assert reserve.await_args.kwargs["attempt_envelope"] == 9
    assert reserve.await_args.kwargs["output_token_limit"] == 2048
    assert len(attempts) == len(requests) == (2 if recover else 3) + int(transport_retry)
    feedback_request = requests[1 + int(transport_retry)]
    feedback = [
        item["output"]
        for item in feedback_request["input"]
        if item.get("type") == "function_call_output"
    ]
    assert len(feedback) == 1
    assert "procedure" in feedback[0] and "model_type" in feedback[0]
    assert all(request["tools"][0]["strict"] is False for request in requests)
