"""Live Amazon Bedrock smoke, skipped unless ``SIBYL_LIVE_BEDROCK_TESTS=1``.

Run it with an AWS identity that may invoke Claude and Cohere, for example::

    SIBYL_LIVE_BEDROCK_TESTS=1 AWS_PROFILE=<profile> AWS_REGION=us-west-2 \\
        uv run pytest tests/ai/test_bedrock_live.py -s

Each call is tiny, so a full run costs a few cents. Evidence lines go to
stdout as JSON; they carry model IDs, request IDs and token counts, never
credentials.
"""

from __future__ import annotations

import json
import math
import os
import sys

import pytest
from pydantic import BaseModel

from sibyl_core.ai import clients, validation
from sibyl_core.ai.bedrock import anthropic_bedrock_client, resolve_bedrock_settings
from sibyl_core.ai.llm import Extractor
from sibyl_core.ai.llm.config import LLMSurface
from sibyl_core.ai.memory_extraction import (
    build_memory_entity_extraction_prompt,
    memory_entity_extractor,
)
from sibyl_core.embeddings.providers import capture_embedding_usage, create_embedding_provider

pytestmark = pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_BEDROCK_TESTS") != "1",
    reason="set SIBYL_LIVE_BEDROCK_TESTS=1 with AWS credentials to call Bedrock",
)


class Verdict(BaseModel):
    ok: bool
    color: str


def evidence(label: str, **fields: object) -> None:
    sys.stdout.write(json.dumps({"live": label, **fields}, default=str) + "\n")


@pytest.fixture(autouse=True)
def fresh_agents(monkeypatch):
    for name in list(os.environ):
        if name.startswith("SIBYL_LLM_"):
            monkeypatch.delenv(name, raising=False)
    clients.invalidate_agent_cache()
    yield
    clients.invalidate_agent_cache()


async def test_opus_5_5_rejects_native_output_so_sibyl_uses_tools_with_high_effort(
    monkeypatch,
):
    from anthropic import BadRequestError
    from anthropic import DefaultAsyncHttpxClient as HttpClient

    settings = resolve_bedrock_settings()
    raw = anthropic_bedrock_client(settings, http_client=HttpClient(), max_retries=0)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    with pytest.raises(BadRequestError) as rejected:
        await raw.messages.create(
            model="us.anthropic.claude-opus-5-5",
            max_tokens=32,
            messages=[{"role": "user", "content": "Return ok=true."}],
            extra_body={
                "output_config": {
                    "effort": "high",
                    "format": {"type": "json_schema", "schema": schema},
                }
            },
        )
    evidence("opus-5-5 native strict", status=rejected.value.status_code, body=rejected.value.body)
    assert "output_config.format" in json.dumps(rejected.value.body)

    monkeypatch.setenv("SIBYL_LLM_MEMORY_PROVIDER", "bedrock")
    monkeypatch.setenv("SIBYL_LLM_MEMORY_MODEL", "claude-opus-5-5")
    extractor = Extractor(
        Verdict,
        surface=LLMSurface.MEMORY,
        system_prompt="Answer as instructed.",
        max_tokens=1024,
    )
    assert await extractor.resolved_effort() == "high"
    assert await extractor.resolved_output_mode() == "tool"
    result = await extractor.extract_with_usage("Report ok=true and color=teal.")
    usage = result.usage
    evidence(
        "opus-5-5 structured via tools",
        output=result.output.model_dump(),
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost_usd,
        request_ids=[attempt.request_id for attempt in usage.transport_attempts],
    )
    assert result.output.ok is True
    assert usage.transport_usage_complete is True
    assert usage.transport_attempts[-1].request_id
    assert usage.cost_usd is not None and usage.cost_usd > 0


async def test_haiku_runs_the_memory_entity_extraction_path(monkeypatch):
    monkeypatch.setenv("SIBYL_LLM_MEMORY_PROVIDER", "bedrock")
    monkeypatch.setenv("SIBYL_LLM_MEMORY_MODEL", "claude-haiku-4-5")
    extractor = memory_entity_extractor(max_tokens=1024)
    prompt = build_memory_entity_extraction_prompt(
        title="Deploy note",
        content=(
            "We moved the Sibyl API to Amazon Bedrock in us-west-2. The worker uses "
            "Cohere Embed v4 for vectors, and SurrealDB stores the graph."
        ),
        max_entities=4,
    )
    result = await extractor.extract_with_usage(prompt)
    usage = result.usage
    names = [entity.name for entity in result.output.entities]
    evidence(
        "haiku memory extraction",
        mode=await extractor.resolved_output_mode(),
        entities=names,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost_usd,
        request_ids=[attempt.request_id for attempt in usage.transport_attempts],
    )
    assert names
    assert usage.transport_usage_complete is True


async def test_probe_answers_without_an_api_key():
    result = await validation.check_provider_key("bedrock", None)
    evidence("probe", status=result.status, model=result.model, latency_ms=result.latency_ms)
    assert result.valid, result.error


async def test_probe_maps_an_unknown_model_to_model_not_found():
    result = await validation.check_model_availability(
        "bedrock", "us.anthropic.claude-nonexistent-9", None
    )
    evidence("bad model", status=result.status, error=result.error)
    assert result.status == "model_not_found"


async def test_cohere_embeds_documents_and_queries_at_1536():
    provider = create_embedding_provider(
        provider="bedrock",
        model="cohere.embed-v4:0",
        dimensions=1536,
        cache_namespace="live",
    )
    documents = [
        "Sibyl stores durable agent memory in a SurrealDB graph.",
        "A sourdough starter needs flour, water and patience.",
    ]
    with capture_embedding_usage(provider) as usage:
        document_vectors = await provider.embed_texts(documents, input_kind="document")
        (query_vector,) = await provider.embed_texts(
            ["where does the agent memory live?"], input_kind="query"
        )
    similarities = [_cosine(query_vector, vector) for vector in document_vectors]
    evidence(
        "cohere embed v4",
        metadata=provider.metadata.to_dict(),
        lengths=[len(vector) for vector in [*document_vectors, query_vector]],
        similarities=similarities,
        usage=usage,
    )
    assert [len(vector) for vector in document_vectors] == [1536, 1536]
    assert len(query_vector) == 1536
    assert provider.metadata.provider == "bedrock"
    assert provider.metadata.model == "cohere.embed-v4:0"
    assert similarities[0] > similarities[1]


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right)))


@pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_BEDROCK_MANTLE") != "1",
    reason="bedrock-mantle needs bedrock-mantle:CreateInference and a Mantle Region",
)
async def test_haiku_through_the_mantle_messages_api(monkeypatch):
    monkeypatch.setenv("SIBYL_BEDROCK_API", "mantle")
    monkeypatch.setenv("SIBYL_LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("SIBYL_LLM_MODEL", "claude-haiku-4-5")
    extractor = Extractor(Verdict, system_prompt="Answer as instructed.", max_tokens=256)
    result = await extractor.extract_with_usage("Report ok=true and color=teal.")
    usage = result.usage
    evidence(
        "mantle haiku",
        output=result.output.model_dump(),
        model=usage.model,
        request_ids=[attempt.request_id for attempt in usage.transport_attempts],
        cost_usd=usage.cost_usd,
    )
    assert result.output.ok is True
