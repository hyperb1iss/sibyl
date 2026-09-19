"""Raw captures missing a vector are repaired, and the raw_vector lane says when they are not."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.services import content_client, content_raw_recall
from sibyl_core.services import content_raw_embedding_repair as repair_module
from sibyl_core.services.content_models import raw_memory_embedding_metadata
from sibyl_core.services.content_raw_embedding_repair import repair_raw_capture_embeddings
from sibyl_core.services.content_raw_recall import RAW_VECTOR_EMBEDDINGS_MISSING
from sibyl_core.services.surreal_content import recall_raw_memory_with_sources, remember_raw_memory
from tests.test_reflection_identity import content_store as content_store


def provider(model: str) -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model=model,
            dimensions=EMBEDDING_DIM,
            cache_namespace=f"raw-repair-{model}",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )


async def remember(org: str, source: str, *, embedding_provider=None, metadata=None):
    return await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id=source,
        raw_content=f"Percent escape handling for {source}",
        embedding_provider=embedding_provider,
        metadata=metadata,
    )


async def stored(memory_id: str) -> dict[str, object]:
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            "SELECT uuid, revision, embedding, metadata.embedding_metadata AS embedding_metadata "
            "FROM raw_captures WHERE uuid = $id;",
            id=memory_id,
        )
    assert len(rows) == 1
    return rows[0]


async def test_repair_is_a_no_op_without_a_configured_provider(monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("must not touch the store"))
    monkeypatch.setattr(content_client, "surreal_content_client", forbidden)
    result = await repair_raw_capture_embeddings("org")
    assert (result.checked, result.recovered, result.pending, result.failed) == (0, 0, 0, 0)
    forbidden.assert_not_called()


async def test_repair_embeds_only_recallable_rows_that_need_it(content_store):
    org = str(uuid4())
    current = provider("current")
    missing = await remember(org, "missing")
    matching = await remember(org, "matching", embedding_provider=current)
    stale = await remember(org, "stale", embedding_provider=provider("previous"))
    deleted = await remember(org, "deleted")
    superseded = await remember(
        org, "superseded", metadata={"superseded_by_source_id": "another-source"}
    )
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET deleted_at = time::now() WHERE uuid = $id;", id=deleted.id
        )
    before = {memory.id: await stored(memory.id) for memory in (missing, matching, stale)}

    result = await repair_raw_capture_embeddings(org, embedding_provider=current)

    assert (result.checked, result.recovered, result.pending, result.failed) == (2, 2, 0, 0)
    expected_metadata = raw_memory_embedding_metadata(current.metadata)
    for memory in (missing, stale):
        row = await stored(memory.id)
        assert row["embedding"] is not None
        assert len(row["embedding"]) == EMBEDDING_DIM
        assert row["embedding_metadata"] == expected_metadata
        assert row["revision"] == before[memory.id]["revision"]
    assert (await stored(stale.id))["embedding"] != before[stale.id]["embedding"]
    assert await stored(matching.id) == before[matching.id]
    for memory in (deleted, superseded):
        assert (await stored(memory.id))["embedding"] is None

    repeated = await repair_raw_capture_embeddings(org, embedding_provider=current)
    assert (repeated.checked, repeated.recovered) == (0, 0)


async def test_repair_pages_past_rows_it_already_handled(content_store):
    org = str(uuid4())
    memories = [await remember(org, f"page-{index}") for index in range(5)]

    result = await repair_raw_capture_embeddings(
        org, page_size=2, embedding_provider=provider("paged")
    )

    assert (result.checked, result.recovered, result.pending, result.failed) == (5, 5, 0, 0)
    for memory in memories:
        assert (await stored(memory.id))["embedding"] is not None


async def test_repair_leaves_a_row_that_moved_under_it_pending(content_store, monkeypatch):
    org = str(uuid4())
    memory = await remember(org, "moving")
    embed = repair_module._raw_memories_with_embeddings

    async def embed_after_a_concurrent_write(memories, embedding_provider):
        async with content_client.surreal_content_client() as client:
            await client.execute_query(
                "UPDATE raw_captures SET revision = revision + 1 WHERE uuid = $id;", id=memory.id
            )
        return await embed(memories, embedding_provider)

    monkeypatch.setattr(
        repair_module, "_raw_memories_with_embeddings", embed_after_a_concurrent_write
    )

    result = await repair_raw_capture_embeddings(org, embedding_provider=provider("moving"))

    assert (result.checked, result.recovered, result.pending, result.failed) == (1, 0, 1, 0)
    assert (await stored(memory.id))["embedding"] is None


async def test_repair_counts_a_provider_failure_without_stopping_the_page_walk(
    content_store, monkeypatch
):
    org = str(uuid4())
    memories = [await remember(org, f"broken-{index}") for index in range(3)]
    monkeypatch.setattr(
        repair_module,
        "_raw_memories_with_embeddings",
        AsyncMock(side_effect=RuntimeError("provider unavailable")),
    )

    result = await repair_raw_capture_embeddings(
        org, page_size=2, embedding_provider=provider("broken")
    )

    assert (result.checked, result.recovered, result.pending, result.failed) == (3, 0, 0, 3)
    for memory in memories:
        assert (await stored(memory.id))["embedding"] is None


async def test_vector_lane_reports_an_unembedded_scope_until_repair_runs(
    content_store, monkeypatch
):
    org = str(uuid4())
    query_provider = provider("lane")
    monkeypatch.setattr(
        content_raw_recall,
        "raw_memory_query_embedding",
        AsyncMock(
            return_value=(await query_provider.embed_texts(["percent"], input_kind="query"))[0]
        ),
    )
    remembered = [await remember(org, f"unembedded-{index}") for index in range(2)]

    async def recall():
        return await recall_raw_memory_with_sources(
            organization_id=org, principal_id="owner", query="percent escape", limit=5
        )

    result = await recall()
    lanes = {source.source: source for source in result.sources}
    assert lanes["raw_fulltext"].failure is None
    assert {memory.id for memory in lanes["raw_fulltext"].candidates} == {
        memory.id for memory in remembered
    }
    assert lanes["raw_vector"].failure is not None
    assert lanes["raw_vector"].failure.error_type == RAW_VECTOR_EMBEDDINGS_MISSING
    assert result.degraded is True
    assert result.as_metadata()["raw_recall_degraded"] is True
    assert len(result.memories) == 2

    repaired = await repair_raw_capture_embeddings(org, embedding_provider=query_provider)
    assert repaired.recovered == 2

    result = await recall()
    lanes = {source.source: source for source in result.sources}
    assert lanes["raw_vector"].failure is None
    assert {memory.id for memory in lanes["raw_vector"].candidates} == {
        memory.id for memory in remembered
    }
    assert result.degraded is False


async def test_vector_lane_stays_healthy_over_an_empty_scope(content_store, monkeypatch):
    query_provider = provider("empty")
    monkeypatch.setattr(
        content_raw_recall,
        "raw_memory_query_embedding",
        AsyncMock(
            return_value=(await query_provider.embed_texts(["percent"], input_kind="query"))[0]
        ),
    )

    result = await recall_raw_memory_with_sources(
        organization_id=str(uuid4()), principal_id="owner", query="percent escape", limit=5
    )

    lanes = {source.source: source for source in result.sources}
    assert lanes["raw_vector"].failure is None
    assert lanes["raw_vector"].candidates == ()
    assert result.degraded is False


async def test_vector_lane_keeps_its_result_when_the_coverage_probe_fails(
    content_store, monkeypatch
):
    org = str(uuid4())
    query_provider = provider("probe")
    monkeypatch.setattr(
        content_raw_recall,
        "raw_memory_query_embedding",
        AsyncMock(
            return_value=(await query_provider.embed_texts(["percent"], input_kind="query"))[0]
        ),
    )
    await remember(org, "probe-failure")
    select_many_raw = content_client.select_many_raw

    async def failing_coverage_probe(client, query, **params):
        if "$coverage_limit" in query:
            raise RuntimeError("coverage probe unavailable")
        return await select_many_raw(client, query, **params)

    monkeypatch.setattr(content_client, "select_many_raw", failing_coverage_probe)

    result = await recall_raw_memory_with_sources(
        organization_id=org, principal_id="owner", query="percent escape", limit=5
    )

    lanes = {source.source: source for source in result.sources}
    assert lanes["raw_vector"].failure is None
    assert lanes["raw_vector"].candidates == ()
    assert result.degraded is False
    assert len(result.memories) == 1


async def test_coverage_judges_eligibility_the_way_recall_does(content_store, monkeypatch):
    """An ineligible row can neither mask a missing vector nor raise the report alone."""
    org = str(uuid4())
    query_provider = provider("eligibility")
    monkeypatch.setattr(
        content_raw_recall,
        "raw_memory_query_embedding",
        AsyncMock(
            return_value=(await query_provider.embed_texts(["percent"], input_kind="query"))[0]
        ),
    )
    ineligible_embedded = await remember(
        org,
        "superseded-with-vector",
        embedding_provider=query_provider,
        metadata={"superseded_by_source_id": "another-source"},
    )
    eligible_unembedded = await remember(org, "eligible-without-vector")

    async def recall(organization_id: str):
        return await recall_raw_memory_with_sources(
            organization_id=organization_id,
            principal_id="owner",
            query="percent escape",
            limit=5,
        )

    result = await recall(org)
    lanes = {source.source: source for source in result.sources}
    assert lanes["raw_vector"].failure is not None
    assert lanes["raw_vector"].failure.error_type == RAW_VECTOR_EMBEDDINGS_MISSING
    assert {memory.id for memory in result.memories} == {eligible_unembedded.id}
    assert ineligible_embedded.id not in {memory.id for memory in result.memories}

    only_ineligible = str(uuid4())
    await remember(
        only_ineligible,
        "superseded-without-vector",
        metadata={"superseded_by_source_id": "another-source"},
    )
    result = await recall(only_ineligible)
    lanes = {source.source: source for source in result.sources}
    assert lanes["raw_vector"].failure is None
    assert lanes["raw_vector"].candidates == ()
    assert result.memories == ()
    assert result.degraded is False
