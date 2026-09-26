"""The raw vector lane and its repair pass against a real SurrealDB 3.x server."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.services import content_client, content_models
from sibyl_core.services.content_models import RawMemory, RawMemoryWrite
from sibyl_core.services.content_raw_embedding_repair import repair_raw_capture_embeddings
from sibyl_core.services.content_raw_recall import RAW_VECTOR_EMBEDDINGS_MISSING
from sibyl_core.services.surreal_content import (
    recall_raw_memory_with_sources,
    remember_raw_memories,
)
from tests.test_surreal_live_runtime import (
    _drop_surreal_namespace,
    _live_surreal_url,
    _surreal_password,
    _surreal_username,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1",
    reason="live SurrealDB runtime smoke tests are disabled",
)

NEAR = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
FAR = [0.0, 1.0, *([0.0] * (EMBEDDING_DIM - 2))]


class StaticProvider:
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


@pytest.fixture
async def live_content(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SurrealContentClient]:
    namespace = f"raw_embedding_space_live_{uuid4().hex}"
    client = SurrealContentClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="content",
    )

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    try:
        await bootstrap_content_schema(client, reset=True)
        yield client
    finally:
        await client.close()
        await _drop_surreal_namespace(namespace)


async def capture(org: str, source: str, provider, *, text: str = "An observation") -> RawMemory:
    [memory] = await remember_raw_memories(
        [
            RawMemoryWrite(
                organization_id=org, principal_id="owner", source_id=source, raw_content=text
            )
        ],
        embedding_provider=provider,
    )
    return memory


async def vector_lane(org: str, *, limit: int = 10):
    result = await recall_raw_memory_with_sources(
        organization_id=org, principal_id="owner", query="unmatched-query-marker", limit=limit
    )
    lanes = {source.source: source for source in result.sources}
    assert lanes["raw_fulltext"].candidates == ()
    return lanes["raw_vector"]


async def test_live_vector_lane_scores_only_the_query_models_captures(
    live_content, monkeypatch
) -> None:
    org = str(uuid4())
    model_a = StaticProvider("openai", "text-embedding-3-small", NEAR)
    model_b = StaticProvider("bedrock", "cohere.embed-v4:0", FAR)
    for index in range(3):
        await capture(org, f"model-a-{index}", model_a)
    from_b = {(await capture(org, f"model-b-{index}", model_b)).id for index in range(2)}
    # Same model, nearest vector, other organization: shares the content namespace.
    await capture(str(uuid4()), "foreign", StaticProvider("bedrock", "cohere.embed-v4:0", NEAR))
    query_model = StaticProvider("bedrock", "cohere.embed-v4:0", NEAR)
    monkeypatch.setattr(
        content_models, "configured_raw_memory_embedding_provider", lambda: query_model
    )

    lane = await vector_lane(org)

    assert {memory.id for memory in lane.candidates} == from_b
    assert lane.failure is None


async def test_live_nearer_old_model_vectors_cannot_crowd_out_the_current_capture(
    live_content, monkeypatch
) -> None:
    org = str(uuid4())
    old_model = StaticProvider("openai", "text-embedding-3-small", NEAR)
    for index in range(64):
        await capture(org, f"old-{index}", old_model)
    current = await capture(org, "current", StaticProvider("bedrock", "cohere.embed-v4:0", FAR))
    query_model = StaticProvider("bedrock", "cohere.embed-v4:0", NEAR)
    monkeypatch.setattr(
        content_models, "configured_raw_memory_embedding_provider", lambda: query_model
    )

    lane = await vector_lane(org, limit=1)

    assert [memory.id for memory in lane.candidates] == [current.id]
    assert lane.failure is None


async def test_live_region_prefixed_bedrock_stamp_matches_the_base_model(
    live_content, monkeypatch
) -> None:
    org = str(uuid4())
    same_model = {
        (await capture(org, source, StaticProvider("bedrock", model, FAR))).id
        for source, model in (
            ("us", "us.cohere.embed-v4:0"),
            ("base", "cohere.embed-v4:0"),
            (
                "profile-arn",
                "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.cohere.embed-v4:0",
            ),
        )
    }
    await capture(org, "other", StaticProvider("openai", "text-embedding-3-small", NEAR))
    query_model = StaticProvider("bedrock", "global.cohere.embed-v4:0", NEAR)
    monkeypatch.setattr(
        content_models, "configured_raw_memory_embedding_provider", lambda: query_model
    )

    lane = await vector_lane(org)

    assert {memory.id for memory in lane.candidates} == same_model


async def test_live_other_model_scope_reports_missing_until_repaired(
    live_content, monkeypatch
) -> None:
    org = str(uuid4())
    for index in range(3):
        await capture(
            org, f"stale-{index}", StaticProvider("openai", "text-embedding-3-small", NEAR)
        )
    current = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="bedrock",
            model="cohere.embed-v4:0",
            dimensions=EMBEDDING_DIM,
            cache_namespace="raw-memory",
            tokenizer_estimate_method="provider-default",
        )
    )
    monkeypatch.setattr(content_models, "configured_raw_memory_embedding_provider", lambda: current)

    lane = await vector_lane(org)
    assert lane.candidates == ()
    assert lane.failure is not None
    assert lane.failure.error_type == RAW_VECTOR_EMBEDDINGS_MISSING

    repaired = await repair_raw_capture_embeddings(org)
    assert (repaired.status, repaired.recovered) == ("completed", 3)

    lane = await vector_lane(org)
    assert len(lane.candidates) == 3
    assert lane.failure is None
