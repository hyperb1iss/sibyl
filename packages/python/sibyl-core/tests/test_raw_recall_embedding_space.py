"""The raw vector lane scores a query only against captures embedded by the query's model."""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import uuid4

import httpx

from sibyl_core.ai.bedrock import BedrockSettings
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.embeddings.bedrock import BedrockEmbeddingProvider
from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.services import content_models
from sibyl_core.services.content_models import RawMemory, RawMemoryWrite
from sibyl_core.services.content_raw_recall import RAW_VECTOR_EMBEDDINGS_MISSING
from sibyl_core.services.surreal_content import (
    recall_raw_memory_with_sources,
    remember_raw_memories,
)
from tests.test_reflection_identity import content_store as content_store

NEAR = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
FAR = [0.0, 1.0, *([0.0] * (EMBEDDING_DIM - 2))]


class StaticProvider:
    """Returns one fixed vector for every text, stamped as the named model."""

    def __init__(self, provider: str, model: str, vector: Sequence[float]) -> None:
        self._vector = list(vector)
        self.metadata = EmbeddingMetadata(
            provider=provider,
            model=model,
            dimensions=len(self._vector),
            cache_namespace="raw-memory",
            tokenizer_estimate_method="provider-default",
        )

    async def embed_texts(self, texts, *, input_kind: str = "document"):
        return [list(self._vector) for _text in texts]


def bedrock(model: str, vector: Sequence[float]) -> BedrockEmbeddingProvider:
    """A real Bedrock provider whose HTTP calls return a fixed vector."""

    def respond(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["texts"]
        return httpx.Response(200, json={"embeddings": {"float": [list(vector) for _ in texts]}})

    return BedrockEmbeddingProvider(
        metadata=EmbeddingMetadata(
            provider="bedrock",
            model=model,
            dimensions=EMBEDDING_DIM,
            cache_namespace="raw-memory",
            tokenizer_estimate_method="provider-default",
        ),
        settings=BedrockSettings(region="us-east-1", api_key="test-key"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )


async def capture(org: str, source: str, provider) -> RawMemory:
    [memory] = await remember_raw_memories(
        [
            RawMemoryWrite(
                organization_id=org,
                principal_id="owner",
                source_id=source,
                raw_content="An observation",
            )
        ],
        embedding_provider=provider,
    )
    return memory


async def recall(org: str, *, limit: int = 10):
    result = await recall_raw_memory_with_sources(
        organization_id=org, principal_id="owner", query="unmatched-query-marker", limit=limit
    )
    return {source.source: source for source in result.sources}


async def test_vector_lane_returns_only_captures_from_the_query_model(content_store, monkeypatch):
    org = str(uuid4())
    # Model A's vectors sit exactly on the query vector, so a lane that ignores
    # the stamp ranks every one of them above model B's captures.
    model_a = StaticProvider("openai", "text-embedding-3-small", NEAR)
    model_b = StaticProvider("bedrock", "cohere.embed-v4:0", FAR)
    from_a = {(await capture(org, f"model-a-{index}", model_a)).id for index in range(3)}
    from_b = {(await capture(org, f"model-b-{index}", model_b)).id for index in range(2)}
    query_model = StaticProvider("bedrock", "cohere.embed-v4:0", NEAR)
    monkeypatch.setattr(
        content_models, "configured_raw_memory_embedding_provider", lambda: query_model
    )

    lanes = await recall(org)

    assert lanes["raw_fulltext"].candidates == ()
    assert {memory.id for memory in lanes["raw_vector"].candidates} == from_b
    assert not from_a & {memory.id for memory in lanes["raw_vector"].candidates}
    assert lanes["raw_vector"].failure is None


async def test_region_prefixed_bedrock_stamps_match_the_base_model(content_store, monkeypatch):
    org = str(uuid4())
    # Stamps naming the model by profile ID and by ARN, one the provider
    # normalized, an opaque profile, and a nearer vector from another model.
    same_model = {
        (await capture(org, source, StaticProvider("bedrock", model, FAR))).id
        for source, model in (
            ("profile-id", "us.cohere.embed-v4:0"),
            (
                "profile-arn",
                "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.cohere.embed-v4:0",
            ),
            ("model-arn", "arn:aws:bedrock:us-east-1::foundation-model/cohere.embed-v4:0"),
        )
    }
    base_stamped = await capture(org, "base-id", bedrock("us.cohere.embed-v4:0", FAR))
    same_model.add(base_stamped.id)
    opaque = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
    await capture(org, "opaque-profile", StaticProvider("bedrock", opaque, NEAR))
    await capture(org, "other-model", StaticProvider("openai", "text-embedding-3-small", NEAR))
    assert base_stamped.metadata["embedding_metadata"]["model"] == "cohere.embed-v4:0"
    query_model = bedrock("global.cohere.embed-v4:0", NEAR)
    monkeypatch.setattr(
        content_models, "configured_raw_memory_embedding_provider", lambda: query_model
    )

    lanes = await recall(org)

    assert {memory.id for memory in lanes["raw_vector"].candidates} == same_model
    assert lanes["raw_vector"].failure is None


async def test_scope_holding_only_another_models_vectors_reports_them_missing(
    content_store, monkeypatch
):
    """A mismatched stamp counts as no vector, so the lane says the scope needs repair."""
    org = str(uuid4())
    stale_model = StaticProvider("openai", "text-embedding-3-small", NEAR)
    for index in range(2):
        await capture(org, f"stale-{index}", stale_model)
    query_model = StaticProvider("bedrock", "cohere.embed-v4:0", NEAR)
    monkeypatch.setattr(
        content_models, "configured_raw_memory_embedding_provider", lambda: query_model
    )

    lanes = await recall(org)

    assert lanes["raw_vector"].candidates == ()
    assert lanes["raw_vector"].failure is not None
    assert lanes["raw_vector"].failure.error_type == RAW_VECTOR_EMBEDDINGS_MISSING


def test_space_normalizes_bedrock_profiles_and_arns_only_for_bedrock() -> None:
    space = content_models.raw_memory_embedding_space
    base = space({"provider": "bedrock", "model": "cohere.embed-v4:0", "dimensions": 1536})
    assert base is not None
    for spelling in (
        "us.cohere.embed-v4:0",
        "global.cohere.embed-v4:0",
        "eu.cohere.embed-v4:0",
        "apac.cohere.embed-v4:0",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.cohere.embed-v4:0",
        "arn:aws:bedrock:us-east-1::foundation-model/cohere.embed-v4:0",
    ):
        assert space({"provider": "bedrock", "model": spelling, "dimensions": 1536}) == base
    assert space({"provider": "bedrock", "model": "cohere.embed-v4:0", "dimensions": 1024}) != base
    assert space({"provider": "openai", "model": "us.model", "dimensions": 8}).model == "us.model"
    assert space(None) is None
    assert space({"provider": "bedrock", "model": "cohere.embed-v4:0"}) is None
    assert space({"provider": "bedrock", "model": "cohere.embed-v4:0", "dimensions": True}) is None


def test_model_keys_cover_profile_spellings_only_for_bedrock() -> None:
    from sibyl_core.services import content_raw_recall

    keys = content_raw_recall._raw_vector_model_keys
    space = content_models.RawEmbeddingSpace
    base = keys(space("bedrock", "cohere.embed-v4:0", 1536))
    assert {"cohere.embed-v4:0", "us.cohere.embed-v4:0", "global.cohere.embed-v4:0"} <= set(base)
    opaque = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
    assert keys(space("bedrock", opaque, 1536)) == ["abc123"]
    # A slash is part of other providers' model names, never an ARN separator.
    assert keys(space("local", "BAAI/bge-small-en-v1.5", 384)) == ["BAAI/bge-small-en-v1.5"]
