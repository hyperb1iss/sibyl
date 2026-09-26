"""Both planes' verdicts weigh each other's pre-upgrade evidence."""

from __future__ import annotations

from sibyl_core.embeddings.provenance import document_chunk_embedding_metadata
from sibyl_core.services import content_client
from sibyl_core.services.embedding_evidence import record_deployment_models
from sibyl_core.services.embedding_sweep import (
    LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE,
    LegacyVectorBasis,
)
from sibyl_core.services.embedding_verdicts import settle_legacy_verdicts
from sibyl_core.services.graph_embedding_sweep import sweep_graph_embeddings
from tests.embedding_upgrade import upgrade_content_to_sweep, upgrade_graph_to_sweep
from tests.test_document_embedding_sweep import (
    CURRENT,
    PREVIOUS,
    ChunkEmbedder,
    _chunk,
    _raw_capture,
)
from tests.test_embedding_sweep import CountingProvider, _entity
from tests.test_embedding_sweep import runtime as runtime
from tests.test_reflection_identity import content_store as content_store


async def _execute(query: str, **params: object) -> object:
    async with content_client.surreal_content_client() as client:
        return await content_client.select_many(client, query, **params)


async def _upgrade(runtime) -> None:
    await upgrade_graph_to_sweep(runtime.client)
    async with content_client.surreal_content_client() as client:
        await upgrade_content_to_sweep(client)


async def _settle(runtime, provider, *, chunk_stamp=CURRENT, allow_unproven=True):
    return await settle_legacy_verdicts(
        runtime.client.group_id,
        graph_client=runtime.client,
        graph_provider=provider,
        chunk_stamp=chunk_stamp,
        embed_chunks=ChunkEmbedder(CURRENT),
        allow_unproven=allow_unproven,
    )


async def test_raw_captures_prove_a_switch_for_a_graph_with_no_stamps(
    runtime, content_store
) -> None:
    org = runtime.client.group_id
    for index in range(3):
        await _entity(runtime, f"legacy-{index}")
    await _raw_capture(org, {**PREVIOUS, "cache_namespace": "raw-memory"})
    await _upgrade(runtime)
    current = CountingProvider("current")

    verdicts = await _settle(runtime, current)

    assert isinstance(verdicts.graph, dict)
    assert verdicts.graph["legacy_basis"] == LegacyVectorBasis.OTHER_PLANE_SWITCHED.value
    assert verdicts.graph.get("legacy_warning") is None
    swept = await sweep_graph_embeddings(runtime, embedding_provider=current)
    assert (swept.recovered, swept.adopted, swept.pending) == (3, 0, 0)


async def test_graph_stamps_prove_a_switch_for_chunks_with_no_raw_captures(
    runtime, content_store
) -> None:
    org = runtime.client.group_id
    previous = CountingProvider("previous").metadata.to_dict()
    await _entity(runtime, "native", stamp=previous)
    await _chunk(org, "legacy-chunk")
    await _upgrade(runtime)

    verdicts = await _settle(runtime, CountingProvider("current"))

    assert isinstance(verdicts.graph, dict)
    assert isinstance(verdicts.document_chunks, dict)
    assert verdicts.graph["legacy_basis"] == LegacyVectorBasis.NO_LEGACY_ROWS.value
    assert verdicts.document_chunks["legacy_basis"] == LegacyVectorBasis.OTHER_PLANE_SWITCHED.value


async def test_no_evidence_anywhere_adopts_with_a_warning_on_both_planes(
    runtime, content_store
) -> None:
    org = runtime.client.group_id
    await _entity(runtime, "legacy")
    await _chunk(org, "legacy-chunk")
    await _upgrade(runtime)
    current = CountingProvider("current")
    # Writes by the upgraded process before its first pass are not evidence.
    await _entity(runtime, "fresh", stamp=current.metadata.to_dict())
    await _chunk(org, "fresh-chunk", stamp=CURRENT)
    await _raw_capture(org, {**CURRENT, "cache_namespace": "raw-memory"})

    # The first attempt waits for the rest of the deployment instead of guessing.
    first = await _settle(runtime, current, allow_unproven=False)
    assert first.deferred
    assert all(
        isinstance(verdict, dict)
        and verdict.get("legacy_deferred")
        and not verdict.get("legacy_decision")
        for verdict in (first.graph, first.document_chunks)
    )
    verdicts = await _settle(runtime, current)

    for verdict in (verdicts.graph, verdicts.document_chunks):
        assert isinstance(verdict, dict)
        assert verdict["legacy_basis"] == LegacyVectorBasis.NO_PRIOR_EVIDENCE.value
        assert verdict["legacy_warning"] == LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE


async def test_the_deployment_record_catches_a_switch_after_the_first_start(
    runtime, content_store
) -> None:
    await _entity(runtime, "legacy")
    await _upgrade(runtime)
    previous = CountingProvider("previous")
    current = CountingProvider("current")
    await record_deployment_models(_execute, graph=previous.metadata.to_dict(), content=PREVIOUS)
    await record_deployment_models(_execute, graph=current.metadata.to_dict(), content=CURRENT)

    verdicts = await _settle(runtime, current)

    assert isinstance(verdicts.graph, dict)
    assert verdicts.graph["legacy_basis"] == LegacyVectorBasis.DEPLOYMENT_MODEL_CHANGED.value


async def test_an_unchanged_deployment_adopts_both_planes_without_a_warning(
    runtime, content_store
) -> None:
    org = runtime.client.group_id
    current = CountingProvider("current")
    await _entity(runtime, "native", stamp=current.metadata.to_dict())
    await _entity(runtime, "legacy")
    await _chunk(org, "legacy-chunk")
    await _raw_capture(org, {**CURRENT, "cache_namespace": "raw-memory"})
    await _upgrade(runtime)

    verdicts = await _settle(runtime, current)

    for verdict in (verdicts.graph, verdicts.document_chunks):
        assert isinstance(verdict, dict)
        assert verdict["legacy_basis"] == LegacyVectorBasis.PRIOR_STAMPS_MATCH.value
        assert verdict.get("legacy_warning") is None


async def test_another_organizations_graph_stamps_speak_for_an_unstamped_graph(
    runtime, content_store
) -> None:
    from sibyl_core.services.embedding_evidence import (
        publish_graph_snapshot,
        read_deployment_graph_snapshot,
    )
    from sibyl_core.services.graph import EntityManager, RelationshipManager, SurrealGraphClient
    from sibyl_core.services.graph_runtime import GraphRuntime, prepare_graph_schema

    current = CountingProvider("current")
    previous = CountingProvider("previous").metadata.to_dict()
    await _entity(runtime, "legacy")
    await _upgrade(runtime)
    native = SurrealGraphClient(group_id=f"native-{runtime.client.group_id}", url="memory://")
    try:
        await prepare_graph_schema(native)
        other = GraphRuntime(
            client=native,
            entity_manager=EntityManager(native, group_id=native.group_id),
            relationship_manager=RelationshipManager(native, group_id=native.group_id),
        )
        await _entity(other, "native", stamp=previous)
        await upgrade_graph_to_sweep(native)

        # The unstamped organization goes first and has nothing to go on yet.
        first = await _settle(runtime, current, allow_unproven=False)
        assert first.deferred
        native_verdicts = await _settle(other, current, allow_unproven=False)
        assert isinstance(native_verdicts.graph, dict)
        assert native_verdicts.graph["legacy_basis"] == LegacyVectorBasis.NO_LEGACY_ROWS.value
        # Publishing is once per namespace.
        assert not await publish_graph_snapshot(
            organization_id=native.group_id,
            graph_execute=native.execute_query,
            content_execute=_execute,
        )
        assert [stamp["model"] for stamp in await read_deployment_graph_snapshot(_execute)] == [
            "previous"
        ]

        settled = await _settle(runtime, current)
    finally:
        await native.close()

    assert isinstance(settled.graph, dict)
    assert settled.graph["legacy_basis"] == LegacyVectorBasis.DEPLOYMENT_STAMPS_DIFFER.value
    assert isinstance(settled.document_chunks, dict)
    assert settled.document_chunks["legacy_basis"] == LegacyVectorBasis.NO_LEGACY_ROWS.value


async def test_a_plane_without_a_provider_is_left_alone(runtime, content_store) -> None:
    await _upgrade(runtime)

    verdicts = await _settle(runtime, None, chunk_stamp=None)

    assert verdicts.graph is None
    assert verdicts.document_chunks is None


async def test_bedrock_chunk_stamps_compare_by_model_not_route(runtime, content_store) -> None:
    org = runtime.client.group_id
    routed = document_chunk_embedding_metadata(
        provider="bedrock", model="us.cohere.embed-v4:0", dimensions=CURRENT["dimensions"]
    )
    await _chunk(org, "legacy-chunk")
    await _raw_capture(org, {**CURRENT, "cache_namespace": "raw-memory"})
    await _upgrade(runtime)

    verdicts = await _settle(runtime, CountingProvider("current"), chunk_stamp=routed)

    assert isinstance(verdicts.document_chunks, dict)
    assert verdicts.document_chunks["legacy_basis"] == LegacyVectorBasis.PRIOR_STAMPS_MATCH.value
