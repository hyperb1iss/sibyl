"""Explicit OpenRouter routing requires encrypted transport to its origin."""

import json
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from sibyl_core.ai.errors import LLMError
from sibyl_core.ai.llm import Extractor
from sibyl_core.ai.llm import extractor as extraction


class Answer(BaseModel):
    answer: str


@pytest.mark.parametrize(
    ("base_url", "routing", "allowed"),
    [
        ("https://openrouter.ai/api/v1", True, True),
        ("https://openrouter.ai:443/api/v1", True, True),
        ("https://openrouter.ai:444/api/v1", True, False),
        ("http://openrouter.ai/api/v1", True, False),
        ("https://other.invalid/v1", True, False),
        ("http://custom.invalid/v1", False, True),
    ],
)
async def test_explicit_routing_requires_https_openrouter(monkeypatch, base_url, routing, allowed):
    reserve = AsyncMock()
    monkeypatch.setattr(extraction, "reserve_llm_budget", reserve)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "response",
                "object": "response",
                "created_at": 0,
                "model": "qwen/qwen3-coder-next",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"answer": "fixture"}),
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        client = AsyncOpenAI(api_key="fixture", base_url=base_url, http_client=http)
        agent = Agent(
            OpenAIResponsesModel(
                "qwen/qwen3-coder-next", provider=OpenAIProvider(openai_client=client)
            ),
            output_type=NativeOutput(Answer, strict=True),
        )
        extractor = Extractor(
            Answer,
            agent=agent,
            output_mode="native_strict",
            openrouter_provider="parasail/bf16" if routing else None,
            max_tokens=128,
        )
        if allowed:
            assert (await extractor.extract_with_usage("fixture")).output.answer == "fixture"
            assert len(requests) == 1
        else:
            with pytest.raises(LLMError) as raised:
                await extractor.extract_with_usage("fixture")
            assert isinstance(raised.value.__cause__, ValueError)
            assert "OpenRouter" in str(raised.value.__cause__)
            assert requests == []
            reserve.assert_not_awaited()
