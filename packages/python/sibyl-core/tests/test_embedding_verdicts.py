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
from tests.embedding_upgrade import (
    previous_release_stamp,
    upgrade_content_to_sweep,
    upgrade_graph_to_sweep,
)
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
    await _raw_capture(org, previous_release_stamp({**PREVIOUS, "cache_namespace": "raw-memory"}))
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
    previous = previous_release_stamp(CountingProvider("previous").metadata.to_dict())
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
    await _entity(runtime, "native", stamp=previous_release_stamp(current.metadata.to_dict()))
    await _entity(runtime, "legacy")
    await _chunk(org, "legacy-chunk")
    await _raw_capture(org, previous_release_stamp({**CURRENT, "cache_namespace": "raw-memory"}))
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
    previous = previous_release_stamp(CountingProvider("previous").metadata.to_dict())
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
    await _raw_capture(org, previous_release_stamp({**CURRENT, "cache_namespace": "raw-memory"}))
    await _upgrade(runtime)

    verdicts = await _settle(runtime, CountingProvider("current"), chunk_stamp=routed)

    assert isinstance(verdicts.document_chunks, dict)
    assert verdicts.document_chunks["legacy_basis"] == LegacyVectorBasis.PRIOR_STAMPS_MATCH.value


async def _rewind(client, name: str, version: int) -> None:
    from sibyl_core.backends.surreal.schema_version import schema_version_record_id

    await client.execute_query(
        "UPDATE type::record($record) SET version = $version;",
        record=schema_version_record_id(name),
        version=version,
    )


async def test_nothing_touches_embedding_evidence_before_the_content_upgrade(
    runtime, content_store
) -> None:
    import pytest

    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
    from sibyl_core.services.content_raw_embedding_repair import (
        REPAIR_SKIPPED_SCHEMA_PENDING,
        repair_raw_capture_embeddings,
    )
    from sibyl_core.services.document_embedding_sweep import sweep_document_chunk_embeddings
    from sibyl_core.services.embedding_sweep import (
        SWEEP_SKIPPED_SCHEMA_PENDING,
        EmbeddingSchemaPendingError,
    )

    org = runtime.client.group_id
    await _raw_capture(org, previous_release_stamp({**PREVIOUS, "cache_namespace": "raw-memory"}))
    async with content_client.surreal_content_client() as client:
        await _rewind(client, "content", 46)
    raw_provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="bedrock",
            model="cohere.embed-v4:0",
            dimensions=CURRENT["dimensions"],
            cache_namespace="raw-memory",
            tokenizer_estimate_method="provider-default",
        )
    )

    repaired = await repair_raw_capture_embeddings(org, embedding_provider=raw_provider)
    with pytest.raises(EmbeddingSchemaPendingError):
        await _settle(runtime, CountingProvider("current"))
    swept = await sweep_document_chunk_embeddings(
        org, stamp=CURRENT, embed_chunks=ChunkEmbedder(CURRENT)
    )

    assert repaired.status == REPAIR_SKIPPED_SCHEMA_PENDING
    assert swept.status == SWEEP_SKIPPED_SCHEMA_PENDING
    stamps = await _execute("SELECT metadata.embedding_metadata.model AS model FROM raw_captures;")
    assert [row["model"] for row in stamps] == [PREVIOUS["model"]]


async def test_a_graph_namespace_before_its_upgrade_publishes_and_decides_nothing(
    runtime, content_store
) -> None:
    from sibyl_core.services.embedding_evidence import read_published_organizations
    from sibyl_core.services.embedding_sweep import (
        SWEEP_SKIPPED_SCHEMA_PENDING,
        EmbeddingSchemaPendingError,
    )

    await _entity(runtime, "legacy")
    await _chunk(runtime.client.group_id, "legacy-chunk")
    await _upgrade(runtime)
    await _rewind(runtime.client, "graph", 30)

    verdicts = await _settle(runtime, CountingProvider("current"))
    swept = await sweep_graph_embeddings(runtime, embedding_provider=CountingProvider("current"))

    assert isinstance(verdicts.graph, EmbeddingSchemaPendingError)
    assert isinstance(verdicts.document_chunks, EmbeddingSchemaPendingError)
    assert await read_published_organizations(_execute) == set()
    assert swept.status == SWEEP_SKIPPED_SCHEMA_PENDING


async def test_stamps_without_a_vector_are_not_evidence(runtime, content_store) -> None:
    from sibyl_core.services.embedding_evidence import read_content_snapshot, read_graph_snapshot

    org = runtime.client.group_id
    current = CountingProvider("current")
    # A client could supply these: neither sits beside a vector.
    await _raw_capture(
        org, previous_release_stamp({**CURRENT, "cache_namespace": "raw-memory"}), vector=False
    )
    await _entity(
        runtime, "planted", stamp=previous_release_stamp(current.metadata.to_dict()), vector=False
    )
    await _entity(runtime, "legacy")
    await _chunk(org, "legacy-chunk")
    await _upgrade(runtime)

    assert await read_content_snapshot(_execute) == []
    assert await read_graph_snapshot(runtime.client.execute_query, org) == []
    verdicts = await _settle(runtime, current)

    for verdict in (verdicts.graph, verdicts.document_chunks):
        assert isinstance(verdict, dict)
        assert verdict["legacy_basis"] == LegacyVectorBasis.NO_PRIOR_EVIDENCE.value


async def test_a_deferral_waits_at_most_the_configured_time(runtime, content_store) -> None:
    await _entity(runtime, "legacy")
    await _upgrade(runtime)
    current = CountingProvider("current")

    first = await _settle(runtime, current, allow_unproven=False)
    again = await settle_legacy_verdicts(
        runtime.client.group_id,
        graph_client=runtime.client,
        graph_provider=current,
        chunk_stamp=None,
        embed_chunks=ChunkEmbedder(CURRENT),
        defer_limit_seconds=3600,
    )
    assert first.deferred
    assert again.deferred
    assert isinstance(again.graph, dict)
    assert again.graph["legacy_deferred_at"] is not None

    # Once the wait is over the plane is settled on what has been published.
    settled = await settle_legacy_verdicts(
        runtime.client.group_id,
        graph_client=runtime.client,
        graph_provider=current,
        chunk_stamp=None,
        embed_chunks=ChunkEmbedder(CURRENT),
        defer_limit_seconds=0,
    )
    assert isinstance(settled.graph, dict)
    assert settled.graph["legacy_basis"] == LegacyVectorBasis.NO_PRIOR_EVIDENCE.value
    assert settled.graph.get("legacy_deferred_at") is None


async def test_other_organizations_matching_stamps_adopt_without_a_warning(
    runtime, content_store
) -> None:
    from sibyl_core.services.embedding_evidence import read_published_organizations
    from sibyl_core.services.graph import EntityManager, RelationshipManager, SurrealGraphClient
    from sibyl_core.services.graph_runtime import GraphRuntime, prepare_graph_schema

    current = CountingProvider("current")
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
        await _entity(other, "native", stamp=previous_release_stamp(current.metadata.to_dict()))
        await upgrade_graph_to_sweep(native)
        await _settle(other, current, allow_unproven=False)
        assert await read_published_organizations(_execute) == {native.group_id}

        verdicts = await _settle(runtime, current, allow_unproven=False)
    finally:
        await native.close()

    assert isinstance(verdicts.graph, dict)
    assert verdicts.graph["legacy_basis"] == LegacyVectorBasis.DEPLOYMENT_STAMPS_MATCH.value
    assert verdicts.graph.get("legacy_warning") is None
    # Another organization's stamps are not this plane's own, so the adoption
    # is reported rather than shown as complete.
    assert verdicts.graph["legacy_notice"] == "adopted_on_deployment_evidence"
    assert await read_published_organizations(_execute) == {
        native.group_id,
        runtime.client.group_id,
    }

    # A switch that replaces every adopted vector retires the notice.
    from sibyl_core.services.embedding_sweep import read_embedding_sweep_state

    adopted = await sweep_graph_embeddings(runtime, embedding_provider=current)
    state = await read_embedding_sweep_state(
        "graph", runtime.client.group_id, runtime.client.execute_query
    )
    assert adopted.adopted == 1
    assert state["legacy_notice"] == "adopted_on_deployment_evidence"
    switched = await sweep_graph_embeddings(runtime, embedding_provider=CountingProvider("next"))
    state = await read_embedding_sweep_state(
        "graph", runtime.client.group_id, runtime.client.execute_query
    )
    assert switched.status == "completed"
    assert state.get("legacy_notice") is None


async def _save_setting(key: str, value: str) -> None:
    await _execute(
        "CREATE system_settings CONTENT {key: $key, value: $value, is_secret: false} RETURN NONE;",
        key=key,
        value=value,
    )


def _space(stamp: dict[str, object]) -> dict[str, object]:
    return {field: stamp[field] for field in ("provider", "model", "dimensions")}


def test_historical_chunk_stamps_mirror_the_previous_crawler() -> None:
    """The previous crawler read the saved setting, then the environment, then its default."""
    from sibyl_core.services.embedding_evidence import historical_chunk_stamps

    raw_default = {"provider": "openai", "model": "text-embedding-3-small", "dimensions": 1536}
    raw_env_model = {**raw_default, "model": "text-embedding-3-large"}

    # No saved setting: the crawler read the same environment as raw captures.
    assert historical_chunk_stamps([raw_env_model], {}) == [raw_env_model]
    # Saved fields override the raw capture's, field by field.
    assert historical_chunk_stamps(
        [raw_env_model], {"provider": "gemini", "model": "gemini-embedding-001"}
    ) == [{"provider": "gemini", "model": "gemini-embedding-001", "dimensions": 1536}]
    assert historical_chunk_stamps([raw_default], {"model": "text-embedding-3-large"}) == [
        {"provider": "openai", "model": "text-embedding-3-large", "dimensions": 1536}
    ]
    assert historical_chunk_stamps([raw_default], {"dimensions": "768"}) == [
        {**raw_default, "dimensions": 768}
    ]
    # A saved provider with no saved model: the environment's model if it set
    # one, otherwise the provider's default.
    assert historical_chunk_stamps([raw_default], {"provider": "gemini"}) == [
        {"provider": "gemini", "model": "gemini-embedding-2", "dimensions": 1536}
    ]
    assert historical_chunk_stamps([raw_env_model], {"provider": "gemini"}) == [
        {"provider": "gemini", "model": "text-embedding-3-large", "dimensions": 1536}
    ]
    # With no raw capture, what the saved settings leave open stays unknown.
    assert historical_chunk_stamps([], {"provider": "openai"}) == [
        {"provider": "openai", "model": None, "dimensions": None}
    ]


async def test_a_saved_crawler_setting_speaks_for_chunks_over_raw_captures(
    runtime, content_store
) -> None:
    """The environment named B for raw captures while the saved setting named A for chunks."""
    org = runtime.client.group_id
    current = CountingProvider("current")
    await _save_setting("embedding_provider", str(PREVIOUS["provider"]))
    await _save_setting("embedding_model", str(PREVIOUS["model"]))
    await _chunk(org, "legacy-chunk")
    await _raw_capture(org, previous_release_stamp({**CURRENT, "cache_namespace": "raw-memory"}))
    await _upgrade(runtime)

    # After the upgrade one resolver picks the environment's B.
    verdicts = await _settle(runtime, current, chunk_stamp=CURRENT)

    assert isinstance(verdicts.document_chunks, dict)
    assert verdicts.document_chunks["legacy_decision"] == "reembed"
    assert verdicts.document_chunks["legacy_basis"] == LegacyVectorBasis.PRIOR_STAMPS_DIFFER.value


async def test_raw_captures_do_not_override_a_matching_saved_crawler_setting(
    runtime, content_store
) -> None:
    org = runtime.client.group_id
    current = CountingProvider("current")
    await _save_setting("embedding_provider", str(PREVIOUS["provider"]))
    await _save_setting("embedding_model", str(PREVIOUS["model"]))
    await _chunk(org, "legacy-chunk")
    await _raw_capture(org, previous_release_stamp({**CURRENT, "cache_namespace": "raw-memory"}))
    await _upgrade(runtime)

    # The operator now configures A everywhere; the chunks were A all along.
    verdicts = await _settle(runtime, current, chunk_stamp=PREVIOUS)

    assert isinstance(verdicts.document_chunks, dict)
    assert verdicts.document_chunks["legacy_decision"] == "adopt"
    assert verdicts.document_chunks["legacy_basis"] == LegacyVectorBasis.PRIOR_STAMPS_MATCH.value
    assert _space(verdicts.document_chunks["legacy_metadata"]) == _space(PREVIOUS)
