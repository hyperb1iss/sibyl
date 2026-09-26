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


class RefusedError(Exception):
    status_code = 400


def _live_repair_provider(model: str) -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model=model,
            dimensions=EMBEDDING_DIM,
            cache_namespace="raw-memory",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )


async def test_live_repair_isolates_a_refused_row_and_resumes_after_its_budget(
    live_content, monkeypatch
) -> None:
    from sibyl_core.backends.surreal.schema_embedding_states import (
        embedding_state_key,
        raw_embedding_refusal_key,
    )
    from sibyl_core.services import content_raw_embedding_repair as repair_module

    org = str(uuid4())
    base = _live_repair_provider("live-repair")
    memories = sorted(
        [await capture(org, f"row-{index}", None, text=f"row {index}") for index in range(6)],
        key=lambda memory: memory.id,
    )
    poisoned = memories[1]
    now = [0.0]

    class Refusing:
        metadata = base.metadata

        async def embed_texts(self, texts, *, input_kind: str = "document"):
            if any(poisoned.raw_content in text for text in texts):
                raise RefusedError("input rejected")
            return await base.embed_texts(texts, input_kind=input_kind)

    # Each walked page costs ten seconds of the budget, whatever the bisection does.
    select_many = content_client.select_many

    async def walking(client, query, **params):
        if "uuid >= $cursor" in query:
            now[0] += 10.0
        return await select_many(client, query, **params)

    monkeypatch.setattr(content_client, "select_many", walking)
    monkeypatch.setattr(repair_module, "_clock", lambda: now[0])
    monkeypatch.setattr(repair_module, "RAW_EMBEDDING_REPAIR_BUDGET_SECONDS", 15.0)

    async def stored() -> dict[str, bool]:
        rows = await select_many(
            live_content,
            "SELECT uuid, embedding != NONE AS embedded FROM raw_captures "
            "WHERE organization_id = $org ORDER BY uuid;",
            org=org,
        )
        return {str(row["uuid"]): bool(row["embedded"]) for row in rows}

    # The first page and its bisection land; the second page's walk crosses
    # the deadline, so its rows wait for the next pass.
    first = await repair_raw_capture_embeddings(org, page_size=3, embedding_provider=Refusing())
    assert first.status == "partial"
    assert first.cursor == memories[2].id
    assert (first.checked, first.recovered, first.refused, first.pending) == (6, 2, 1, 3)
    embedded_first = {memories[0].id, memories[2].id}
    assert await stored() == {memory.id: memory.id in embedded_first for memory in memories}
    refusal = await select_many(
        live_content,
        "SELECT capture_id, organization_id, status_code FROM type::record($key);",
        key=raw_embedding_refusal_key(org, poisoned.id),
    )
    assert refusal == [{"capture_id": poisoned.id, "organization_id": org, "status_code": 400}]
    state = await select_many(
        live_content,
        "SELECT cursors, lease_owner FROM type::record($key);",
        key=embedding_state_key(org, repair_module.RAW_CAPTURE_EMBEDDING_PLANE),
    )
    assert state[0]["cursors"] == {repair_module.RAW_CAPTURE_EMBEDDING_PLANE: memories[2].id}
    assert state[0].get("lease_owner") is None

    monkeypatch.setattr(repair_module, "RAW_EMBEDDING_REPAIR_BUDGET_SECONDS", 60.0)
    second = await repair_raw_capture_embeddings(org, page_size=3, embedding_provider=Refusing())
    assert (second.status, second.cursor) == ("completed", "")
    assert (second.recovered, second.refused) == (3, 1)
    assert await stored() == {memory.id: memory is not poisoned for memory in memories}


async def test_live_second_repair_of_an_organization_waits_for_the_lease(live_content) -> None:
    import asyncio

    org = str(uuid4())
    await capture(org, "early", None, text="early row")
    await capture(org, "late", None, text="late row")
    parked = asyncio.Event()
    release = asyncio.Event()
    embedded: list[str] = []
    base = _live_repair_provider("live-lease")

    class Gated:
        metadata = base.metadata

        async def embed_texts(self, texts, *, input_kind: str = "document"):
            if not parked.is_set():
                parked.set()
                await release.wait()
            embedded.extend(texts)
            return await base.embed_texts(texts, input_kind=input_kind)

    first = asyncio.create_task(repair_raw_capture_embeddings(org, embedding_provider=Gated()))
    await asyncio.wait_for(parked.wait(), 10)
    second = await repair_raw_capture_embeddings(org, embedding_provider=Gated())
    release.set()
    first_result = await first

    assert second.status == "busy"
    assert (first_result.status, first_result.recovered) == ("completed", 2)
    assert sorted(embedded) == ["early row", "late row"], "each row was paid for once"


async def test_live_a_row_the_model_keeps_failing_on_is_deferred_not_refused(
    live_content,
) -> None:
    from sibyl_core.backends.surreal.schema_embedding_states import raw_embedding_refusal_key

    class ModelFailedError(Exception):
        status_code = 424

    org = str(uuid4())
    failing_row = await capture(org, "failing", None, text="the model fails on this")
    for index in range(3):
        await capture(org, f"fine-{index}", None, text=f"fine row {index}")
    base = _live_repair_provider("live-defer")
    requests: list[list[str]] = []

    class FailsOnOneRow:
        metadata = base.metadata

        async def embed_texts(self, texts, *, input_kind: str = "document"):
            requests.append(list(texts))
            if "the model fails on this" in texts:
                raise ModelFailedError("model failed")
            return await base.embed_texts(texts, input_kind=input_kind)

    first = await repair_raw_capture_embeddings(org, embedding_provider=FailsOnOneRow())
    held = len(requests)
    second = await repair_raw_capture_embeddings(org, embedding_provider=FailsOnOneRow())
    stored = await content_client.select_many(
        live_content,
        "SELECT kind, attempts, capture_id FROM type::record($key);",
        key=raw_embedding_refusal_key(org, failing_row.id),
    )

    assert (first.recovered, first.deferred, first.refused) == (3, 1, 0)
    assert (second.deferred, second.refused) == (1, 0)
    assert len(requests) == held, "a held deferral is not sent again"
    assert stored == [{"kind": "deferred", "attempts": 1, "capture_id": failing_row.id}]
