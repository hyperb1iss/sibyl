"""Graph vectors follow the configured model without re-embedding what is already right."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.schema_embedding_states import REOPEN_EMBEDDING_STATES
from sibyl_core.embeddings.provenance import (
    UNVERIFIED_ORIGIN_ARCHIVE,
    is_rate_limit_error,
    unverified_embedding_metadata,
)
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import embedding_sweep as sweep_module
from sibyl_core.services.embedding_sweep import (
    SWEEP_BUSY,
    SWEEP_COMPLETED,
    SWEEP_CURRENT,
    SWEEP_PARTIAL,
    SWEEP_SKIPPED_DIMENSION_MISMATCH,
    SWEEP_SKIPPED_NO_PROVIDER,
    LegacyEvidence,
    LegacyVectorBasis,
    LegacyVectorDecision,
    decide_legacy_vectors,
    read_embedding_sweep_state,
)
from sibyl_core.services.graph import EntityManager, RelationshipManager, SurrealGraphClient
from sibyl_core.services.graph_embedding_sweep import (
    GRAPH_EMBEDDING_PLANE,
    entity_row_embedding_text,
    sweep_graph_embeddings,
)
from sibyl_core.services.graph_runtime import GraphRuntime, prepare_graph_schema


class CountingProvider(DeterministicEmbeddingProvider):
    """Deterministic vectors that also count how many texts it was asked for."""

    def __init__(self, model: str, *, dimensions: int = EMBEDDING_DIM) -> None:
        super().__init__(
            EmbeddingMetadata(
                provider="deterministic",
                model=model,
                dimensions=dimensions,
                cache_namespace="graph",
                tokenizer_estimate_method="utf8-byte-length",
            )
        )
        self.texts: list[str] = []

    async def embed_texts(self, texts: Sequence[str], *, input_kind="document"):
        self.texts.extend(texts)
        return await super().embed_texts(texts, input_kind=input_kind)


class ThrottleError(Exception):
    status_code = 429


@pytest.fixture
async def runtime() -> AsyncIterator[GraphRuntime]:
    client = SurrealGraphClient(group_id=f"sweep-{uuid4().hex}", url="memory://")
    try:
        await prepare_graph_schema(client)
        yield GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=client.group_id),
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
    finally:
        await client.close()


def _vector(seed: float) -> list[float]:
    return [seed, *([0.0] * (EMBEDDING_DIM - 1))]


async def _entity(runtime: GraphRuntime, entity_id: str, *, stamp=None, vector=True) -> None:
    metadata = {"summary": f"summary of {entity_id}"}
    if stamp is not None:
        metadata["embedding_metadata"] = stamp
    await runtime.entity_manager.create_direct(
        Entity(
            id=entity_id,
            entity_type=EntityType.TOPIC,
            name=f"Entity {entity_id}",
            description=f"Description of {entity_id}",
            metadata=metadata,
            embedding=_vector(0.5) if vector else None,
        ),
        generate_embedding=False,
    )


async def _relationship(runtime: GraphRuntime, rel_id: str, source: str, target: str, *, stamp):
    metadata: dict[str, object] = {"fact": f"{source} relates to {target}"}
    metadata["fact_embedding"] = _vector(0.25)
    if stamp is not None:
        metadata["embedding_metadata"] = stamp
    await runtime.relationship_manager.create_direct_bulk(
        [
            Relationship(
                id=rel_id,
                relationship_type=RelationshipType.RELATED_TO,
                source_id=source,
                target_id=target,
                metadata=metadata,
            )
        ],
        generate_embeddings=False,
    )


async def _rows(runtime: GraphRuntime, table: str) -> dict[str, dict[str, object]]:
    vector = "name_embedding" if table == "entity" else "fact_embedding"
    rows = normalize_records(
        await runtime.client.execute_query(
            f"SELECT uuid, {vector} AS vector, attributes.embedding_metadata AS stamp, "
            f"revision, updated_at FROM {table} WHERE group_id = $group_id;",
            group_id=runtime.client.group_id,
        )
    )
    return {str(row["uuid"]): row for row in rows}


def test_legacy_verdict_prefers_the_operator_then_the_plane_evidence() -> None:
    differs = LegacyEvidence(differs=True, matches=True)
    matches = LegacyEvidence(differs=False, matches=True)
    silent = LegacyEvidence(differs=False, matches=False)

    assert decide_legacy_vectors(legacy_rows=False, evidence=differs, policy="auto") == (
        LegacyVectorDecision.NONE,
        LegacyVectorBasis.NO_LEGACY_ROWS,
    )
    assert decide_legacy_vectors(legacy_rows=True, evidence=differs, policy="auto") == (
        LegacyVectorDecision.REEMBED,
        LegacyVectorBasis.PRIOR_STAMPS_DIFFER,
    )
    assert decide_legacy_vectors(legacy_rows=True, evidence=matches, policy="auto") == (
        LegacyVectorDecision.ADOPT,
        LegacyVectorBasis.PRIOR_STAMPS_MATCH,
    )
    assert decide_legacy_vectors(legacy_rows=True, evidence=silent, policy="auto") == (
        LegacyVectorDecision.ADOPT,
        LegacyVectorBasis.NO_PRIOR_EVIDENCE,
    )
    assert decide_legacy_vectors(legacy_rows=True, evidence=matches, policy="reembed") == (
        LegacyVectorDecision.REEMBED,
        LegacyVectorBasis.OPERATOR_REEMBED,
    )
    assert decide_legacy_vectors(legacy_rows=True, evidence=differs, policy="adopt") == (
        LegacyVectorDecision.ADOPT,
        LegacyVectorBasis.OPERATOR_ADOPT,
    )


def test_rate_limit_errors_are_recognized_through_their_cause() -> None:
    class BotoLike(Exception):
        def __init__(self) -> None:
            super().__init__("throttled")
            self.response = {"Error": {"Code": "ThrottlingException"}}

    wrapped = RuntimeError("provider call failed")
    wrapped.__cause__ = ThrottleError()

    assert is_rate_limit_error(ThrottleError())
    assert is_rate_limit_error(BotoLike())
    assert is_rate_limit_error(wrapped)
    assert not is_rate_limit_error(ValueError("bad input"))


async def test_plain_upgrade_adopts_unstamped_vectors_without_embedding(runtime) -> None:
    provider = CountingProvider("current")
    stamp = provider.metadata.to_dict()
    await _entity(runtime, "stamped", stamp=stamp)
    await _entity(runtime, "legacy-a")
    await _entity(runtime, "legacy-b")
    await _entity(runtime, "lexical", vector=False)
    await _relationship(runtime, "edge-legacy", "legacy-a", "legacy-b", stamp=None)
    before = await _rows(runtime, "entity")

    result = await sweep_graph_embeddings(runtime, embedding_provider=provider)

    assert result.status == SWEEP_COMPLETED
    assert result.legacy_decision == LegacyVectorDecision.ADOPT.value
    assert (result.adopted, result.recovered, result.pending, result.failed) == (3, 0, 0, 0)
    assert provider.texts == []
    entities = await _rows(runtime, "entity")
    for entity_id in ("stamped", "legacy-a", "legacy-b"):
        assert entities[entity_id]["stamp"] == stamp
        assert entities[entity_id]["vector"] == before[entity_id]["vector"]
        assert entities[entity_id]["revision"] == before[entity_id]["revision"]
    # A row that never had a vector stays lexical: the sweep repairs models, not coverage.
    assert entities["lexical"]["vector"] is None
    assert entities["lexical"]["stamp"] is None
    assert (await _rows(runtime, "relates_to"))["edge-legacy"]["stamp"] == stamp
    state = await read_embedding_sweep_state(
        GRAPH_EMBEDDING_PLANE, runtime.client.group_id, runtime.client.execute_query
    )
    assert state["legacy_basis"] == LegacyVectorBasis.PRIOR_STAMPS_MATCH.value
    assert state["complete_metadata"] == stamp

    repeated = await sweep_graph_embeddings(runtime, embedding_provider=provider)
    assert repeated.status == SWEEP_CURRENT
    assert provider.texts == []


async def test_switch_during_upgrade_reembeds_old_stamps_and_unstamped_vectors(runtime) -> None:
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    await _entity(runtime, "old", stamp=previous.metadata.to_dict())
    await _entity(runtime, "legacy")
    await _entity(runtime, "fresh", stamp=current.metadata.to_dict())
    await _relationship(runtime, "edge-old", "old", "legacy", stamp=previous.metadata.to_dict())
    await _relationship(runtime, "edge-legacy", "legacy", "fresh", stamp=None)
    before = await _rows(runtime, "entity")

    result = await sweep_graph_embeddings(runtime, embedding_provider=current)

    assert result.status == SWEEP_COMPLETED
    assert result.legacy_decision == LegacyVectorDecision.REEMBED.value
    assert (result.adopted, result.recovered, result.pending, result.failed) == (0, 4, 0, 0)
    stamp = current.metadata.to_dict()
    entities = await _rows(runtime, "entity")
    for entity_id in ("old", "legacy"):
        assert entities[entity_id]["stamp"] == stamp
        assert entities[entity_id]["vector"] != before[entity_id]["vector"]
        # A sweep is not an edit: recency and optimistic concurrency stay put.
        assert entities[entity_id]["revision"] == before[entity_id]["revision"]
        assert entities[entity_id]["updated_at"] == before[entity_id]["updated_at"]
    assert entities["fresh"]["vector"] == before["fresh"]["vector"]
    edges = await _rows(runtime, "relates_to")
    assert {edge["stamp"]["model"] for edge in edges.values()} == {"current"}
    # Only stale rows reached the provider, each exactly once.
    assert len(current.texts) == 4


async def test_legacy_verdict_is_decided_once_even_after_the_evidence_is_gone(
    runtime,
) -> None:
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    await _entity(runtime, "old", stamp=previous.metadata.to_dict())
    for index in range(4):
        await _entity(runtime, f"legacy-{index}")

    first = await sweep_graph_embeddings(runtime, embedding_provider=current, budget_seconds=0.0)
    assert first.status == SWEEP_PARTIAL
    # The stamp that proved a switch is replaced before the legacy rows are,
    # but the persisted verdict keeps them from being adopted as current.
    await runtime.client.execute_query(
        "UPDATE entity SET attributes.embedding_metadata = $stamp WHERE uuid = 'old';",
        stamp=current.metadata.to_dict(),
    )
    finished = await sweep_graph_embeddings(runtime, embedding_provider=current)

    assert finished.status == SWEEP_COMPLETED
    assert finished.adopted == 0
    assert finished.recovered == 4


async def test_operator_policy_reembeds_vectors_without_evidence(runtime, monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_EMBEDDING_LEGACY_VECTORS", "reembed")
    provider = CountingProvider("current")
    await _entity(runtime, "legacy")

    result = await sweep_graph_embeddings(runtime, embedding_provider=provider)

    assert result.legacy_decision == LegacyVectorDecision.REEMBED.value
    assert (result.adopted, result.recovered) == (0, 1)
    state = await read_embedding_sweep_state(
        GRAPH_EMBEDDING_PLANE, runtime.client.group_id, runtime.client.execute_query
    )
    assert state["legacy_basis"] == LegacyVectorBasis.OPERATOR_REEMBED.value


async def test_sweep_resumes_from_its_cursor_after_a_restart(runtime) -> None:
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(9):
        await _entity(runtime, f"row-{index}", stamp=previous.metadata.to_dict())

    embed = current.embed_texts
    calls = 0

    async def interrupted(texts, *, input_kind="document"):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return await embed(texts, input_kind=input_kind)

    current.embed_texts = interrupted  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await sweep_graph_embeddings(
            runtime, embedding_provider=current, page_size=3, batch_size=3, concurrency=1
        )
    state = await read_embedding_sweep_state(
        GRAPH_EMBEDDING_PLANE, runtime.client.group_id, runtime.client.execute_query
    )
    # The interrupted pass released its lease and kept the rows it finished.
    assert state.get("lease_owner") is None
    assert len(current.texts) == 3

    current.embed_texts = embed  # type: ignore[method-assign]
    resumed = await sweep_graph_embeddings(
        runtime, embedding_provider=current, page_size=3, batch_size=3, concurrency=1
    )

    assert resumed.status == SWEEP_COMPLETED
    assert resumed.recovered == 6
    assert len(current.texts) == 9
    stamps = {row["stamp"]["model"] for row in (await _rows(runtime, "entity")).values()}
    assert stamps == {"current"}


async def test_a_row_edited_mid_pass_keeps_its_new_text_and_is_retried(runtime) -> None:
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    await _entity(runtime, "moving", stamp=previous.metadata.to_dict())
    embed = current.embed_texts

    async def edit_while_embedding(texts, *, input_kind="document"):
        await runtime.client.execute_query(
            "UPDATE entity SET name = 'Renamed' WHERE uuid = 'moving';"
        )
        current.embed_texts = embed  # type: ignore[method-assign]
        return await embed(texts, input_kind=input_kind)

    current.embed_texts = edit_while_embedding  # type: ignore[method-assign]
    first = await sweep_graph_embeddings(runtime, embedding_provider=current)

    assert (first.recovered, first.skipped) == (0, 1)
    assert (await _rows(runtime, "entity"))["moving"]["stamp"]["model"] == "previous"

    second = await sweep_graph_embeddings(runtime, embedding_provider=current)
    assert second.recovered == 1
    assert current.texts[-1] == entity_row_embedding_text(
        {
            "uuid": "moving",
            "entity_type": "topic",
            "name": "Renamed",
            "description": "Description of moving",
            "summary": "summary of moving",
        }
    )


async def test_vectors_cleared_by_a_dimension_rebuild_are_regenerated(runtime) -> None:
    provider = CountingProvider("current")
    await _entity(runtime, "cleared", stamp=provider.metadata.to_dict())
    await runtime.client.execute_query("UPDATE entity SET name_embedding = NONE;")

    result = await sweep_graph_embeddings(runtime, embedding_provider=provider)

    assert result.recovered == 1
    assert (await _rows(runtime, "entity"))["cleared"]["vector"] is not None


async def test_dimension_mismatch_is_refused_before_any_embedding(runtime) -> None:
    provider = CountingProvider("wide", dimensions=EMBEDDING_DIM + 8)
    await _entity(runtime, "old", stamp=CountingProvider("old").metadata.to_dict())

    result = await sweep_graph_embeddings(runtime, embedding_provider=provider)

    assert result.status == SWEEP_SKIPPED_DIMENSION_MISMATCH
    assert (result.provider_dimensions, result.schema_dimensions) == (
        EMBEDDING_DIM + 8,
        EMBEDDING_DIM,
    )
    assert provider.texts == []


async def test_no_provider_means_no_sweep(runtime) -> None:
    result = await sweep_graph_embeddings(runtime, embedding_provider=None)
    assert result.status == SWEEP_SKIPPED_NO_PROVIDER


async def test_an_overlapping_pass_yields_to_the_lease_holder(runtime) -> None:
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    await _entity(runtime, "row", stamp=previous.metadata.to_dict())
    embed = current.embed_texts
    release = asyncio.Event()
    entered = asyncio.Event()

    async def slow(texts, *, input_kind="document"):
        entered.set()
        await release.wait()
        return await embed(texts, input_kind=input_kind)

    current.embed_texts = slow  # type: ignore[method-assign]
    holder = asyncio.create_task(sweep_graph_embeddings(runtime, embedding_provider=current))
    await entered.wait()
    overlapping = await sweep_graph_embeddings(runtime, embedding_provider=current)
    release.set()
    finished = await holder

    assert overlapping.status == SWEEP_BUSY
    assert finished.recovered == 1


async def test_throttling_halves_concurrency_and_retries_the_batch(runtime, monkeypatch) -> None:
    monkeypatch.setattr(sweep_module, "_BACKOFF_BASE_SECONDS", 0.01)
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(4):
        await _entity(runtime, f"row-{index}", stamp=previous.metadata.to_dict())
    embed = current.embed_texts
    throttled = 0

    async def throttle_once(texts, *, input_kind="document"):
        nonlocal throttled
        if throttled == 0:
            throttled += 1
            raise RuntimeError("wrapped") from ThrottleError()
        return await embed(texts, input_kind=input_kind)

    current.embed_texts = throttle_once  # type: ignore[method-assign]
    result = await sweep_graph_embeddings(
        runtime, embedding_provider=current, page_size=4, batch_size=2, concurrency=2
    )

    assert throttled == 1
    assert (result.recovered, result.failed) == (4, 0)


async def test_an_import_reopens_a_finished_plane(runtime) -> None:
    provider = CountingProvider("current")
    await _entity(runtime, "known", stamp=provider.metadata.to_dict())
    assert (await sweep_graph_embeddings(runtime, embedding_provider=provider)).status == (
        SWEEP_COMPLETED
    )
    await _entity(
        runtime, "imported", stamp=unverified_embedding_metadata(UNVERIFIED_ORIGIN_ARCHIVE)
    )
    assert (await sweep_graph_embeddings(runtime, embedding_provider=provider)).status == (
        SWEEP_CURRENT
    )

    await runtime.client.execute_query(
        REOPEN_EMBEDDING_STATES, organizations=[runtime.client.group_id]
    )
    reopened = await sweep_graph_embeddings(runtime, embedding_provider=provider)

    assert reopened.status == SWEEP_COMPLETED
    assert reopened.recovered == 1
    assert (await _rows(runtime, "entity"))["imported"]["stamp"] == provider.metadata.to_dict()


async def test_operator_reembed_replaces_every_vector_including_current_ones(runtime) -> None:
    from sibyl_core.services.graph_embedding_sweep import mark_graph_embeddings_for_reembed

    provider = CountingProvider("current")
    await _entity(runtime, "current", stamp=provider.metadata.to_dict())
    await _entity(runtime, "legacy")
    await _entity(runtime, "lexical", vector=False)
    assert (await sweep_graph_embeddings(runtime, embedding_provider=provider)).status == (
        SWEEP_COMPLETED
    )

    marked = await mark_graph_embeddings_for_reembed(runtime.client)
    again = await mark_graph_embeddings_for_reembed(runtime.client)
    result = await sweep_graph_embeddings(runtime, embedding_provider=provider)

    assert (marked, again) == (2, 0)
    assert result.status == SWEEP_COMPLETED
    assert result.recovered == 2
    entities = await _rows(runtime, "entity")
    assert entities["lexical"]["stamp"] is None
    assert entities["current"]["stamp"] == provider.metadata.to_dict()
