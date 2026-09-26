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
from sibyl_core.services.content_raw_recall import (
    RAW_VECTOR_COVERAGE_UNKNOWN,
    RAW_VECTOR_EMBEDDINGS_MISSING,
)
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
    assert result.status == repair_module.REPAIR_SKIPPED_NO_PROVIDER
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


async def test_coverage_walks_past_ineligible_rows_before_judging(content_store, monkeypatch):
    """A crowd of ineligible unembedded rows cannot hide one eligible row behind the pool."""
    org = str(uuid4())
    query_provider = provider("crowd")
    monkeypatch.setattr(
        content_raw_recall,
        "raw_memory_query_embedding",
        AsyncMock(
            return_value=(await query_provider.embed_texts(["percent"], input_kind="query"))[0]
        ),
    )
    eligible = await remember(org, "older-eligible")
    for index in range(150):
        await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id=f"crowd-{index}",
            raw_content=f"Unrelated archived note {index}",
            embedding_provider=None,
            metadata={"superseded_by_source_id": "another-source"},
        )

    result = await recall_raw_memory_with_sources(
        organization_id=org, principal_id="owner", query="percent escape", limit=5
    )

    lanes = {source.source: source for source in result.sources}
    assert {memory.id for memory in result.memories} == {eligible.id}
    assert lanes["raw_vector"].failure is not None
    assert lanes["raw_vector"].failure.error_type == RAW_VECTOR_EMBEDDINGS_MISSING


async def test_coverage_reports_unknown_when_its_walk_is_inconclusive(content_store, monkeypatch):
    """A walk that hits its row cap without a verdict neither asserts missing nor certifies healthy."""
    org = str(uuid4())
    query_provider = provider("capped")
    monkeypatch.setattr(
        content_raw_recall,
        "raw_memory_query_embedding",
        AsyncMock(
            return_value=(await query_provider.embed_texts(["percent"], input_kind="query"))[0]
        ),
    )
    await remember(org, "eligible-behind-the-cap")
    sides: list[str] = []

    async def capped_walk(client, *, extra_clause, **kwargs):
        sides.append(extra_clause)
        return False if "!= NONE" in extra_clause else None

    monkeypatch.setattr(content_raw_recall, "_eligible_rows_present", capped_walk)

    result = await recall_raw_memory_with_sources(
        organization_id=org, principal_id="owner", query="percent escape", limit=1
    )

    lanes = {source.source: source for source in result.sources}
    assert sides == [" AND embedding != NONE", " AND embedding = NONE"]
    assert lanes["raw_vector"].failure is None
    assert lanes["raw_vector"].candidates == ()
    assert lanes["raw_vector"].note == RAW_VECTOR_COVERAGE_UNKNOWN
    assert result.degraded is False
    assert result.as_metadata()["raw_recall_notes"] == {"raw_vector": RAW_VECTOR_COVERAGE_UNKNOWN}


async def test_coverage_walk_never_reads_past_its_row_cap(monkeypatch):
    """Every page is clamped to the remaining budget, and the walk stops at the cap."""
    monkeypatch.setattr(content_raw_recall, "_COVERAGE_ROW_CAP", 300)
    requested: list[int] = []

    def ineligible_row(index: int) -> dict[str, object]:
        return {
            "uuid": f"{index:08d}",
            "organization_id": "org",
            "source_id": f"source-{index}",
            "principal_id": "owner",
            "raw_content": "archived",
            "revision": 1,
            "metadata": {"superseded_by_source_id": "another-source"},
        }

    async def full_pages(client, query, **params):
        limit = int(params["coverage_limit"])
        requested.append(limit)
        start = len(requested) * 1000
        return [ineligible_row(start + index) for index in range(limit)]

    monkeypatch.setattr(content_client, "select_many_raw", full_pages)

    verdict = await content_raw_recall._eligible_rows_present(
        object(),
        where_clause="organization_id = $organization_id",
        params={"organization_id": "org"},
        as_of=None,
        extra_clause=" AND embedding = NONE",
        page_size=128,
    )

    assert verdict is None
    assert requested == [128, 128, 44]
    assert sum(requested) == 300


async def test_walk_leaves_text_and_vectors_on_the_server(content_store, monkeypatch):
    """The per-minute walk projects only decision columns and fetches text for candidates."""
    org = str(uuid4())
    current = provider("lean")
    await remember(org, "needs-vector")
    await remember(org, "has-vector", embedding_provider=current)
    queries: list[str] = []
    select_many = content_client.select_many

    async def recording(client, query, **params):
        queries.append(query)
        return await select_many(client, query, **params)

    monkeypatch.setattr(content_client, "select_many", recording)

    result = await repair_raw_capture_embeddings(org, embedding_provider=current)

    assert (result.checked, result.recovered) == (1, 1)
    walks = [q for q in queries if "uuid >= $cursor" in q]
    assert walks
    for walk in walks:
        projection = walk.split(" FROM ", 1)[0]
        assert "raw_content" not in projection
        assert "embedding" not in projection
        assert "embedding = NONE OR metadata.embedding_metadata = NONE" in walk
        assert "metadata.embedding_metadata.model != $expected_metadata.model" in walk
    fetches = [q for q in queries if "uuid IN $ids" in q]
    assert len(fetches) == 1
    assert "raw_content" in fetches[0]


async def test_fully_current_org_issues_no_embed_call(content_store, monkeypatch):
    org = str(uuid4())
    current = provider("settled")
    for index in range(3):
        await remember(org, f"settled-{index}", embedding_provider=current)
    embed = AsyncMock(side_effect=AssertionError("nothing to embed"))
    monkeypatch.setattr(repair_module, "_raw_memories_with_embeddings", embed)
    fetch = AsyncMock(side_effect=AssertionError("nothing to fetch"))
    monkeypatch.setattr(repair_module, "_repair_page", fetch)

    result = await repair_raw_capture_embeddings(org, embedding_provider=current)

    assert result.status == repair_module.REPAIR_COMPLETED
    assert (result.checked, result.recovered, result.pending, result.failed) == (0, 0, 0, 0)
    embed.assert_not_called()
    fetch.assert_not_called()


async def test_repair_refuses_a_provider_whose_dimensions_do_not_fit_the_schema(monkeypatch):
    mismatched = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="narrow",
            dimensions=EMBEDDING_DIM // 2,
            cache_namespace="raw-repair-narrow",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    forbidden = AsyncMock(side_effect=AssertionError("must not touch the store"))
    monkeypatch.setattr(content_client, "surreal_content_client", forbidden)
    embed = AsyncMock(side_effect=AssertionError("must not embed"))
    monkeypatch.setattr(repair_module, "_raw_memories_with_embeddings", embed)

    result = await repair_raw_capture_embeddings("org", embedding_provider=mismatched)

    assert result.status == repair_module.REPAIR_SKIPPED_DIMENSION_MISMATCH
    assert (result.provider_dimensions, result.schema_dimensions) == (
        EMBEDDING_DIM // 2,
        EMBEDDING_DIM,
    )
    assert (result.checked, result.recovered, result.pending, result.failed) == (0, 0, 0, 0)
    forbidden.assert_not_called()
    embed.assert_not_called()


async def test_a_legacy_stamp_naming_the_configured_model_is_restamped_without_embedding(
    content_store, monkeypatch
):
    """After the upgrade, a previous-release stamp that already names the model is rewritten
    in the current format; only a stamp naming another model costs an embedding call."""
    from tests.embedding_upgrade import previous_release_stamp

    org = str(uuid4())
    current = provider("steady")
    memory = await remember(org, "legacy", embedding_provider=current)
    legacy = previous_release_stamp(raw_memory_embedding_metadata(current.metadata))
    async with content_client.surreal_content_client() as client:
        await content_client.select_many(
            client,
            "UPDATE raw_captures SET metadata.embedding_metadata = $stamp WHERE uuid = $id;",
            stamp=legacy,
            id=memory.id,
        )
    before = await stored(memory.id)
    embed = AsyncMock(side_effect=AssertionError("a matching model needs no embedding call"))
    monkeypatch.setattr(repair_module, "_raw_memories_with_embeddings", embed)

    result = await repair_raw_capture_embeddings(org, embedding_provider=current)

    after = await stored(memory.id)
    assert (result.checked, result.recovered) == (1, 1)
    embed.assert_not_called()
    assert after["embedding_metadata"] == raw_memory_embedding_metadata(current.metadata)
    assert after["embedding"] == before["embedding"]
    # A second pass finds nothing left to do.
    assert (await repair_raw_capture_embeddings(org, embedding_provider=current)).checked == 0


async def test_legacy_stamps_are_restamped_a_page_per_statement(content_store, monkeypatch):
    """A plain upgrade rewrites every legacy raw stamp; one statement per page, not per row."""
    from tests.embedding_upgrade import previous_release_stamp

    org = str(uuid4())
    current = provider("steady")
    memories = [
        await remember(org, f"legacy {index}", embedding_provider=current) for index in range(12)
    ]
    legacy = previous_release_stamp(raw_memory_embedding_metadata(current.metadata))
    async with content_client.surreal_content_client() as client:
        await content_client.select_many(
            client,
            "UPDATE raw_captures SET metadata.embedding_metadata = $stamp "
            "WHERE organization_id = $org;",
            stamp=legacy,
            org=org,
        )
    statements: list[str] = []
    original = content_client.select_many

    async def recording(client, query, **params):
        statements.append(query)
        return await original(client, query, **params)

    monkeypatch.setattr(content_client, "select_many", recording)
    monkeypatch.setattr(
        repair_module,
        "_raw_memories_with_embeddings",
        AsyncMock(side_effect=AssertionError("a matching model needs no embedding call")),
    )

    result = await repair_raw_capture_embeddings(org, embedding_provider=current, page_size=5)

    assert (result.checked, result.recovered, result.pending) == (12, 12, 0)
    restamps = [query for query in statements if "SET metadata.embedding_metadata" in query]
    # Twelve rows in pages of five: three statements, each finding rows by uuid.
    assert len(restamps) == 3
    assert all("WHERE uuid IN $uuids" in query for query in restamps)
    for memory in memories:
        assert (await stored(memory.id))["embedding_metadata"] == raw_memory_embedding_metadata(
            current.metadata
        )
