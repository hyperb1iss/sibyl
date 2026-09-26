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
    is_transient_provider_error,
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
from tests.embedding_upgrade import previous_release_stamp, upgrade_graph_to_sweep


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


async def _seed_many(runtime: GraphRuntime, count: int, *, stamp) -> None:
    await runtime.entity_manager.create_direct_bulk(
        [
            Entity(
                id=f"row-{index:03d}",
                entity_type=EntityType.TOPIC,
                name=f"Entity row-{index:03d}",
                metadata={"embedding_metadata": stamp},
                embedding=_vector(0.5),
            )
            for index in range(count)
        ]
    )


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
    # Without stamps of its own, a plane follows the deployment record, then
    # the other plane; its own stamps outrank both.
    assert decide_legacy_vectors(
        legacy_rows=True, evidence=LegacyEvidence(deployment_differs=True), policy="auto"
    ) == (LegacyVectorDecision.REEMBED, LegacyVectorBasis.DEPLOYMENT_STAMPS_DIFFER)
    assert decide_legacy_vectors(
        legacy_rows=True, evidence=LegacyEvidence(model_changed=True), policy="auto"
    ) == (LegacyVectorDecision.REEMBED, LegacyVectorBasis.DEPLOYMENT_MODEL_CHANGED)
    assert decide_legacy_vectors(
        legacy_rows=True, evidence=LegacyEvidence(other_plane_switched=True), policy="auto"
    ) == (LegacyVectorDecision.REEMBED, LegacyVectorBasis.OTHER_PLANE_SWITCHED)
    # Any switch outranks any continuity: conflicting evidence re-embeds.
    assert decide_legacy_vectors(
        legacy_rows=True,
        evidence=LegacyEvidence(matches=True, other_plane_switched=True),
        policy="auto",
    ) == (LegacyVectorDecision.REEMBED, LegacyVectorBasis.OTHER_PLANE_SWITCHED)
    assert decide_legacy_vectors(
        legacy_rows=True,
        evidence=LegacyEvidence(matches=True, model_changed=True),
        policy="auto",
    ) == (LegacyVectorDecision.REEMBED, LegacyVectorBasis.DEPLOYMENT_MODEL_CHANGED)
    assert decide_legacy_vectors(
        legacy_rows=True,
        evidence=LegacyEvidence(matches=True, deployment_differs=True),
        policy="auto",
    ) == (LegacyVectorDecision.REEMBED, LegacyVectorBasis.DEPLOYMENT_STAMPS_DIFFER)
    # Another organization's matching stamps adopt without a warning.
    assert decide_legacy_vectors(
        legacy_rows=True, evidence=LegacyEvidence(deployment_matches=True), policy="auto"
    ) == (LegacyVectorDecision.ADOPT, LegacyVectorBasis.DEPLOYMENT_STAMPS_MATCH)
    assert decide_legacy_vectors(legacy_rows=True, evidence=matches, policy="reembed") == (
        LegacyVectorDecision.REEMBED,
        LegacyVectorBasis.OPERATOR_REEMBED,
    )
    assert decide_legacy_vectors(legacy_rows=True, evidence=differs, policy="adopt") == (
        LegacyVectorDecision.ADOPT,
        LegacyVectorBasis.OPERATOR_ADOPT,
    )


def test_transient_provider_errors_are_recognized_through_their_cause() -> None:
    class BotoLike(Exception):
        def __init__(self, code: str) -> None:
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    class ServerError(Exception):
        status_code = 503

    class BadRequest(Exception):
        status_code = 400

    wrapped = RuntimeError("provider call failed")
    wrapped.__cause__ = ThrottleError()

    assert is_transient_provider_error(ThrottleError())
    assert is_transient_provider_error(BotoLike("ThrottlingException"))
    assert is_transient_provider_error(BotoLike("ServiceUnavailableException"))
    assert is_transient_provider_error(BotoLike("ModelNotReadyException"))
    assert is_transient_provider_error(ServerError())
    assert is_transient_provider_error(wrapped)
    # A refused input is not a capacity signal; the sweep bisects those instead.
    assert not is_transient_provider_error(BotoLike("ValidationException"))
    assert not is_transient_provider_error(BadRequest())
    assert not is_transient_provider_error(ValueError("bad input"))


async def test_plain_upgrade_adopts_unstamped_vectors_without_embedding(runtime) -> None:
    provider = CountingProvider("current")
    stamp = provider.metadata.to_dict()
    await _entity(runtime, "stamped", stamp=previous_release_stamp(stamp))
    await _entity(runtime, "legacy-a")
    await _entity(runtime, "legacy-b")
    await _entity(runtime, "lexical", vector=False)
    await _relationship(runtime, "edge-legacy", "legacy-a", "legacy-b", stamp=None)
    await upgrade_graph_to_sweep(runtime.client)
    before = await _rows(runtime, "entity")

    result = await sweep_graph_embeddings(runtime, embedding_provider=provider)

    assert result.status == SWEEP_COMPLETED
    assert result.legacy_decision == LegacyVectorDecision.ADOPT.value
    assert result.warning is None
    assert (result.adopted, result.recovered, result.pending, result.failed) == (3, 0, 0, 0)
    assert provider.texts == []
    entities = await _rows(runtime, "entity")
    # The stamped row keeps the previous release's stamp: its model matches.
    assert entities["stamped"]["stamp"] == previous_release_stamp(stamp)
    for entity_id in ("stamped", "legacy-a", "legacy-b"):
        if entity_id != "stamped":
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
    await _entity(runtime, "old", stamp=previous_release_stamp(previous.metadata.to_dict()))
    await _entity(runtime, "legacy")
    await _relationship(
        runtime,
        "edge-old",
        "old",
        "legacy",
        stamp=previous_release_stamp(previous.metadata.to_dict()),
    )
    await upgrade_graph_to_sweep(runtime.client)
    # Written by the upgraded process before its first pass.
    await _entity(runtime, "fresh", stamp=current.metadata.to_dict())
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
    await _entity(runtime, "old", stamp=previous_release_stamp(previous.metadata.to_dict()))
    for index in range(4):
        await _entity(runtime, f"legacy-{index}")
    await upgrade_graph_to_sweep(runtime.client)

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


async def test_a_new_namespace_records_an_empty_snapshot(runtime) -> None:
    from sibyl_core.services.embedding_evidence import read_graph_snapshot

    state = await read_embedding_sweep_state(
        GRAPH_EMBEDDING_PLANE, runtime.client.group_id, runtime.client.execute_query
    )

    assert state["legacy_evidence"]["stamps"] == []
    assert await read_graph_snapshot(runtime.client.execute_query, runtime.client.group_id) == []


async def test_the_upgrade_photographs_stamps_once_by_model(runtime) -> None:
    from sibyl_core.backends.surreal.schema_embedding_states import (
        snapshot_graph_embedding_evidence,
    )
    from sibyl_core.services.embedding_evidence import read_graph_snapshot

    previous = previous_release_stamp(CountingProvider("previous").metadata.to_dict())
    await _entity(runtime, "old-a", stamp=previous)
    await _entity(runtime, "old-b", stamp=previous)
    await _entity(
        runtime, "unknown", stamp=unverified_embedding_metadata(UNVERIFIED_ORIGIN_ARCHIVE)
    )
    await _relationship(runtime, "edge-old", "old-a", "old-b", stamp=previous)
    await upgrade_graph_to_sweep(runtime.client)
    await _entity(runtime, "after", stamp=CountingProvider("current").metadata.to_dict())
    # A second run, as after an interrupted bootstrap, keeps the first photograph.
    await snapshot_graph_embedding_evidence(
        runtime.client.execute_query, group_id=runtime.client.group_id
    )

    stamps = await read_graph_snapshot(runtime.client.execute_query, runtime.client.group_id)

    assert sorted((stamp["model"], stamp["rows"]) for stamp in stamps) == [
        ("previous", 1),
        ("previous", 2),
    ]


async def test_writes_after_the_upgrade_are_not_evidence(runtime) -> None:
    current = CountingProvider("current")
    for index in range(3):
        await _entity(runtime, f"legacy-{index}")
    await upgrade_graph_to_sweep(runtime.client)
    # The upgraded process writes before its first pass; that stamp matches
    # the configured model but says nothing about the older vectors.
    await _entity(runtime, "fresh", stamp=current.metadata.to_dict())

    result = await sweep_graph_embeddings(runtime, embedding_provider=current)

    state = await read_embedding_sweep_state(
        GRAPH_EMBEDDING_PLANE, runtime.client.group_id, runtime.client.execute_query
    )
    assert state["legacy_basis"] == LegacyVectorBasis.NO_PRIOR_EVIDENCE.value
    assert state["legacy_warning"] == sweep_module.LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE
    assert result.warning == sweep_module.LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE
    assert result.adopted == 3


async def test_an_operator_reembed_clears_the_adoption_warning(runtime) -> None:
    from sibyl_core.services.graph_embedding_sweep import (
        count_graph_embeddings_for_reembed,
        mark_graph_embeddings_for_reembed,
    )

    current = CountingProvider("current")
    for index in range(3):
        await _entity(runtime, f"legacy-{index}")
    await _entity(runtime, "lexical", vector=False)
    await upgrade_graph_to_sweep(runtime.client)
    adopted = await sweep_graph_embeddings(runtime, embedding_provider=current)
    assert adopted.warning == sweep_module.LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE

    assert await count_graph_embeddings_for_reembed(runtime.client) == 3
    assert await mark_graph_embeddings_for_reembed(runtime.client) == 3
    replaced = await sweep_graph_embeddings(runtime, embedding_provider=current)

    assert replaced.warning is None
    assert (replaced.recovered, replaced.pending) == (3, 0)
    assert len(current.texts) == 3


async def test_only_fields_that_shape_the_vector_trigger_a_reembed(runtime) -> None:
    current = CountingProvider("current")
    stamp = current.metadata.to_dict()
    await _entity(runtime, "bookkeeping", stamp={**stamp, "cache_namespace": "renamed"})
    await _entity(runtime, "estimator", stamp={**stamp, "tokenizer_estimate_method": "other"})
    await _entity(runtime, "text", stamp={**stamp, "text_version": "native-graph-v0"})
    await _entity(runtime, "input-kind", stamp={**stamp, "input_kind_sensitive": False})
    await upgrade_graph_to_sweep(runtime.client)

    result = await sweep_graph_embeddings(runtime, embedding_provider=current)

    assert result.status == SWEEP_COMPLETED
    assert result.recovered == 2
    assert len(current.texts) == 2
    entities = await _rows(runtime, "entity")
    assert entities["bookkeeping"]["stamp"]["cache_namespace"] == "renamed"
    assert entities["estimator"]["stamp"]["tokenizer_estimate_method"] == "other"
    assert entities["text"]["stamp"] == stamp
    assert entities["input-kind"]["stamp"] == stamp
    # A finished plane stays current when only bookkeeping differs.
    assert (
        await sweep_graph_embeddings(
            runtime,
            embedding_provider=CountingProvider("current"),
        )
    ).status == SWEEP_CURRENT


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
    from sibyl_core.backends.surreal.schema import rebuild_embedding_indexes_for_dimension
    from sibyl_core.embeddings.provenance import UNVERIFIED_ORIGIN_REBUILD

    provider = CountingProvider("current")
    await _entity(runtime, "stamped", stamp=provider.metadata.to_dict())
    await _entity(runtime, "legacy")
    await _relationship(runtime, "edge-legacy", "stamped", "legacy", stamp=None)
    await _entity(runtime, "lexical", vector=False)

    await rebuild_embedding_indexes_for_dimension(runtime.client, dimension=EMBEDDING_DIM)
    cleared = await _rows(runtime, "entity")

    # The rebuild empties every vector but leaves each one a stamp to be found by.
    assert cleared["stamped"]["vector"] is None
    assert cleared["legacy"]["vector"] is None
    assert cleared["legacy"]["stamp"]["origin"] == UNVERIFIED_ORIGIN_REBUILD
    assert cleared["lexical"]["stamp"] is None

    result = await sweep_graph_embeddings(runtime, embedding_provider=provider)

    assert (result.recovered, result.pending) == (3, 0)
    entities = await _rows(runtime, "entity")
    for entity_id in ("stamped", "legacy"):
        assert entities[entity_id]["vector"] is not None
        assert entities[entity_id]["stamp"] == provider.metadata.to_dict()
    assert entities["lexical"]["vector"] is None
    assert (await _rows(runtime, "relates_to"))["edge-legacy"]["vector"] is not None


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
    # The refusal leaves a receipt, so status surfaces say why nothing moves.
    state = await read_embedding_sweep_state(
        GRAPH_EMBEDDING_PLANE, runtime.client.group_id, runtime.client.execute_query
    )
    assert state["last_run"]["status"] == SWEEP_SKIPPED_DIMENSION_MISMATCH
    assert state["active_metadata"]["dimensions"] == EMBEDDING_DIM + 8


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


async def test_a_row_the_provider_rejects_does_not_hold_back_its_batch(runtime) -> None:
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(8):
        await _entity(runtime, f"row-{index}", stamp=previous.metadata.to_dict())
    embed = current.embed_texts

    async def reject_one(texts, *, input_kind="document"):
        if any("Entity row-5" in text for text in texts):
            raise ValueError("input rejected by provider")
        return await embed(texts, input_kind=input_kind)

    current.embed_texts = reject_one  # type: ignore[method-assign]
    result = await sweep_graph_embeddings(
        runtime, embedding_provider=current, page_size=8, batch_size=8, concurrency=1
    )

    # The bad row is isolated by halving; every other row in its batch lands.
    assert (result.recovered, result.failed) == (7, 1)
    stamps = {key: row["stamp"]["model"] for key, row in (await _rows(runtime, "entity")).items()}
    assert stamps.pop("row-5") == "previous"
    assert set(stamps.values()) == {"current"}
    # The refusal is remembered, so the plane completes and the next pass does
    # not send the same text again.
    assert result.status == SWEEP_COMPLETED
    sent = len(current.texts)
    again = await sweep_graph_embeddings(
        runtime, embedding_provider=current, verify_interval_seconds=0
    )
    assert (again.rejected, again.recovered, again.failed) == (1, 0, 0)
    assert len(current.texts) == sent
    # A changed text is new input and is tried again.
    await runtime.client.execute_query(
        "UPDATE entity SET description = 'Rewritten' WHERE uuid = 'row-5';"
    )
    current.embed_texts = embed  # type: ignore[method-assign]
    retried = await sweep_graph_embeddings(
        runtime, embedding_provider=current, verify_interval_seconds=0
    )
    assert (retried.rejected, retried.recovered) == (0, 1)


async def test_rejected_rows_at_any_density_do_not_stall_a_healthy_provider(runtime) -> None:
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    bad = {f"row-{index:03d}" for index in range(0, 200, 10)}
    await _seed_many(runtime, 200, stamp=previous.metadata.to_dict())
    embed = current.embed_texts

    async def reject_tenth(texts, *, input_kind="document"):
        if any(text.split("\n")[1].removeprefix("Entity ") in bad for text in texts):
            raise ValueError("input rejected by provider")
        return await embed(texts, input_kind=input_kind)

    current.embed_texts = reject_tenth  # type: ignore[method-assign]
    result = await sweep_graph_embeddings(runtime, embedding_provider=current)

    # Concurrent bisection fails at every level above the clean halves; the
    # provider is still judged up, and every row it accepts lands.
    assert result.status != "provider_failing"
    assert (result.recovered, result.failed) == (180, 20)
    stamps = await _rows(runtime, "entity")
    assert all(
        (stamps[row_id]["stamp"]["model"] == "previous") == (row_id in bad) for row_id in stamps
    )


async def test_a_provider_that_is_down_stops_the_pass_quickly(runtime) -> None:
    from sibyl_core.services.embedding_sweep import SWEEP_PROVIDER_FAILING

    previous = CountingProvider("previous")
    current = CountingProvider("current")
    await _seed_many(runtime, 160, stamp=previous.metadata.to_dict())
    calls = 0

    async def down(texts, *, input_kind="document"):
        nonlocal calls
        calls += 1
        raise PermissionError("credentials rejected")

    current.embed_texts = down  # type: ignore[method-assign]
    result = await sweep_graph_embeddings(
        runtime, embedding_provider=current, page_size=32, batch_size=8, concurrency=4
    )

    assert result.status == SWEEP_PROVIDER_FAILING
    assert result.recovered == 0
    # A bounded number of calls, not one per row, and rows never tried alone
    # are pending rather than failed.
    assert calls <= 128 + 4
    assert result.failed < 128
    assert result.pending == 160


async def test_a_batch_that_times_out_is_split_until_it_fits(runtime, monkeypatch) -> None:
    monkeypatch.setattr(sweep_module, "_EMBED_CALL_TIMEOUT_SECONDS", 0.2)
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(16):
        await _entity(runtime, f"row-{index:02d}", stamp=previous.metadata.to_dict())
    embed = current.embed_texts

    async def slow_when_large(texts, *, input_kind="document"):
        if len(texts) > 4:
            await asyncio.sleep(5)  # stands in for a provider choking on a large request
        return await embed(texts, input_kind=input_kind)

    current.embed_texts = slow_when_large  # type: ignore[method-assign]
    result = await sweep_graph_embeddings(
        runtime, embedding_provider=current, page_size=16, batch_size=16, concurrency=1
    )

    assert result.status == SWEEP_COMPLETED
    assert (result.recovered, result.failed, result.skipped) == (16, 0, 0)


async def test_a_reembed_during_a_pass_is_not_undone_when_that_pass_ends(runtime) -> None:
    from sibyl_core.services.graph_embedding_sweep import mark_graph_embeddings_for_reembed

    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(4):
        await _entity(runtime, f"row-{index}", stamp=previous.metadata.to_dict())
    embed = current.embed_texts
    marked = False

    async def refuse_then_reopen(texts, *, input_kind="document"):
        nonlocal marked
        if any("Entity row-2" in text for text in texts):
            raise ValueError("input too long")
        if any("Entity row-3" in text for text in texts) and not marked:
            # The operator asks for a re-embed while this pass holds the lease.
            marked = True
            await mark_graph_embeddings_for_reembed(runtime.client)
        return await embed(texts, input_kind=input_kind)

    current.embed_texts = refuse_then_reopen  # type: ignore[method-assign]
    await sweep_graph_embeddings(
        runtime, embedding_provider=current, page_size=1, batch_size=1, concurrency=1
    )
    state = await read_embedding_sweep_state(
        GRAPH_EMBEDDING_PLANE, runtime.client.group_id, runtime.client.execute_query
    )
    assert not (state.get("rejections") or {}).get("rows")

    current.embed_texts = embed  # type: ignore[method-assign]
    healed = await sweep_graph_embeddings(runtime, embedding_provider=current)
    assert healed.rejected == 0
    assert (await _rows(runtime, "entity"))["row-2"]["stamp"]["model"] == "current"


def test_every_kind_of_dropped_connection_or_timeout_is_transient() -> None:
    class ConnectTimeout(Exception):
        pass

    assert is_transient_provider_error(ConnectionRefusedError("refused"))
    assert is_transient_provider_error(ConnectionResetError("reset"))
    assert is_transient_provider_error(TimeoutError())
    assert is_transient_provider_error(ConnectTimeout("connect timed out"))


def test_the_embedding_stamp_shape_is_pinned() -> None:
    """The stamp every stored graph vector carries.

    The sweep and the vector lanes compare only the fields that shape a vector
    (``VECTOR_IDENTITY_FIELDS``), so a bookkeeping field can move freely, but a
    change to one of those re-embeds every graph vector in every deployment.
    The stamp version marks stamps this release writes; stamps without it are
    the previous release's, the only ones counted as pre-upgrade evidence.
    """
    stamp = EmbeddingMetadata(
        provider="p",
        model="m",
        dimensions=8,
        cache_namespace="graph",
        tokenizer_estimate_method="t",
    ).to_dict()

    assert set(stamp) == {
        "provider",
        "model",
        "dimensions",
        "cache_namespace",
        "tokenizer_estimate_method",
        "text_version",
        "normalize",
        "input_kind_sensitive",
        "stamp_version",
    }
    assert (
        stamp["text_version"],
        stamp["normalize"],
        stamp["input_kind_sensitive"],
        stamp["stamp_version"],
    ) == ("native-graph-v1", True, True, 2)


async def test_a_store_that_refuses_writes_stops_the_pass(runtime, monkeypatch) -> None:
    from sibyl_core.services.embedding_sweep import SWEEP_STORE_FAILING, SweepTable

    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(40):
        await _entity(runtime, f"row-{index:02d}", stamp=previous.metadata.to_dict())

    def refusing_write(self: SweepTable) -> str:
        return "THROW 'field refuses this vector';"

    monkeypatch.setattr(SweepTable, "write_query", refusing_write)
    result = await sweep_graph_embeddings(
        runtime, embedding_provider=current, page_size=8, batch_size=4, concurrency=1
    )

    # Three refused writes end the pass (batches already in flight on that page
    # finish) instead of paying for all 40 vectors.
    assert result.status == SWEEP_STORE_FAILING
    assert len(current.texts) <= 16
    assert result.recovered == 0


async def test_a_provider_stuck_unavailable_backs_off_instead_of_bisecting(
    runtime, monkeypatch
) -> None:
    from sibyl_core.services.embedding_sweep import SWEEP_PROVIDER_FAILING

    monkeypatch.setattr(sweep_module, "_BACKOFF_BASE_SECONDS", 0.05)

    class Unavailable(Exception):
        status_code = 503

    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(16):
        await _entity(runtime, f"row-{index:02d}", stamp=previous.metadata.to_dict())
    sizes: list[int] = []

    async def unavailable(texts, *, input_kind="document"):
        sizes.append(len(texts))
        raise Unavailable("service unavailable")

    current.embed_texts = unavailable  # type: ignore[method-assign]
    result = await sweep_graph_embeddings(
        runtime,
        embedding_provider=current,
        page_size=16,
        batch_size=16,
        concurrency=1,
        budget_seconds=1.0,
    )

    assert result.status == SWEEP_PROVIDER_FAILING
    # Every retry resends the whole batch: a brownout is not split into more calls.
    assert set(sizes) == {16}
    assert (result.failed, result.rejected) == (0, 0)


async def test_an_operator_reembed_retries_rows_the_provider_refused(runtime) -> None:
    from sibyl_core.services.graph_embedding_sweep import mark_graph_embeddings_for_reembed

    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(4):
        await _entity(runtime, f"row-{index}", stamp=previous.metadata.to_dict())
    embed = current.embed_texts

    async def refuse_row_2(texts, *, input_kind="document"):
        if any("Entity row-2" in text for text in texts):
            raise ValueError("input too long")
        return await embed(texts, input_kind=input_kind)

    current.embed_texts = refuse_row_2  # type: ignore[method-assign]
    first = await sweep_graph_embeddings(runtime, embedding_provider=current)
    assert (first.recovered, first.failed) == (3, 1)

    # The provider was fixed; the operator asks for everything again.
    current.embed_texts = embed  # type: ignore[method-assign]
    await mark_graph_embeddings_for_reembed(runtime.client)
    again = await sweep_graph_embeddings(runtime, embedding_provider=current)

    assert (again.rejected, again.recovered) == (0, 4)


async def test_pending_counts_only_refusals_that_are_still_stale(runtime) -> None:
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(4):
        await _entity(runtime, f"bad-{index}", stamp=previous.metadata.to_dict())
    embed = current.embed_texts

    async def refuse_bad(texts, *, input_kind="document"):
        if any("Entity bad-" in text for text in texts) and len(texts) == 1:
            raise ValueError("refused")
        if any("Entity bad-" in text for text in texts):
            raise ValueError("refused")
        return await embed(texts, input_kind=input_kind)

    await _entity(runtime, "good", stamp=previous.metadata.to_dict())
    current.embed_texts = refuse_bad  # type: ignore[method-assign]
    await sweep_graph_embeddings(runtime, embedding_provider=current)

    # The refused rows go away and new stale rows arrive; nothing may hide them.
    await runtime.client.execute_query("DELETE entity WHERE string::starts_with(uuid, 'bad-');")
    for index in range(3):
        await _entity(runtime, f"new-{index}", stamp=previous.metadata.to_dict())
    partial = await sweep_graph_embeddings(
        runtime, embedding_provider=current, budget_seconds=0.0, verify_interval_seconds=0
    )

    assert partial.pending == 3


async def test_a_row_the_store_refuses_does_not_hold_back_its_batch(runtime, monkeypatch) -> None:
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    for index in range(12):
        await _entity(runtime, f"row-{index:02d}", stamp=previous.metadata.to_dict())
    real_rows = sweep_module._rows

    async def refuse_row_07(plane, query, **params):
        if "SET name_embedding" in query and "row-07" in params.get("uuids", ()):
            raise RuntimeError("row-07 violates the current schema")
        return await real_rows(plane, query, **params)

    monkeypatch.setattr(sweep_module, "_rows", refuse_row_07)
    result = await sweep_graph_embeddings(
        runtime, embedding_provider=current, page_size=12, batch_size=12, concurrency=1
    )

    # One provider call; the refused write is halved until only row-07 is left.
    assert len(current.texts) == 12
    assert (result.recovered, result.failed) == (11, 1)
    assert result.status != "store_failing"


async def test_one_busy_answer_near_the_deadline_is_not_an_outage(runtime, monkeypatch) -> None:
    monkeypatch.setattr(sweep_module, "_BACKOFF_BASE_SECONDS", 5.0)
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    await _entity(runtime, "row", stamp=previous.metadata.to_dict())

    async def busy(texts, *, input_kind="document"):
        raise ThrottleError("slow down")

    current.embed_texts = busy  # type: ignore[method-assign]
    result = await sweep_graph_embeddings(runtime, embedding_provider=current, budget_seconds=0.5)

    assert result.status == SWEEP_PARTIAL
    assert result.skipped == 1


def test_connection_drops_are_transient() -> None:
    class ConnectError(Exception):
        pass

    assert is_transient_provider_error(ConnectionError("reset by peer"))
    assert is_transient_provider_error(ConnectError("connection refused"))


def test_bedrock_chunk_stamps_name_the_model_not_the_route() -> None:
    from sibyl_core.embeddings.provenance import document_chunk_embedding_metadata

    stamps = {
        document_chunk_embedding_metadata(provider="bedrock", model=model, dimensions=1536)["model"]
        for model in (
            "cohere.embed-v4:0",
            "us.cohere.embed-v4:0",
            "global.cohere.embed-v4:0",
            "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.cohere.embed-v4:0",
        )
    }
    assert stamps == {"cohere.embed-v4:0"}
