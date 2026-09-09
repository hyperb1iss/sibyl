"""Graph publication must keep the source observations used by its plan."""

from sibyl_core.models.entities import EntityType
from sibyl_core.services import content_client
from sibyl_core.services.memory_sharing import _apply_share_plan, _resolve_raw_memory_share_plan
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_synthesis_source_observations import (
    content_store as content_store,
)
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)
from tests.test_synthesis_source_observations import (
    runtime as runtime,
)


async def share_plan(runtime, memory_id):
    return await _resolve_raw_memory_share_plan(
        raw_memory_id=memory_id,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        promote_to_scope="project",
        promote_to_scope_key="project_a",
        accessible_projects={"project_a"},
    )


async def publish(runtime, plan):
    return await _apply_share_plan(
        plan=plan,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        domain=None,
        related_to=None,
        accessible_projects={"project_a"},
        writable_projects={"project_a"},
        accessible_teams=set(),
        accessible_delegations=set(),
    )


async def test_share_plan_captures_durable_source_observation(runtime, content_store):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="share-observation",
        raw_content="The deployment rule is blue approval.",
        embedding_provider=None,
    )
    plan = await share_plan(runtime, source.id)
    assert plan.source_observations
    observation = plan.source_observations[0]
    assert observation.durable
    assert observation.source.id == source.id
    assert observation.source.organization_id == runtime.client.group_id
    result = await publish(runtime, plan)
    assert result.success


async def test_share_plan_cannot_adopt_identical_recreated_source(runtime, content_store):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="share-recreation",
        raw_content="The deployment rule is blue approval.",
        embedding_provider=None,
    )
    plan = await share_plan(runtime, source.id)
    async with content_client.surreal_content_client() as client:
        rows = await client.execute_query(
            "SELECT * FROM raw_captures WHERE uuid=$uuid;", uuid=source.id
        )
        saved = {key: value for key, value in rows[0].items() if key != "id"}
        await client.execute_query("DELETE raw_captures WHERE uuid=$uuid;", uuid=source.id)
        await client.execute_query("CREATE raw_captures CONTENT $saved;", saved=saved)
    result = await publish(runtime, plan)
    assert not result.success
    assert not await runtime.entity_manager.list_by_type(EntityType.EPISODE, limit=20)


async def test_graph_derivation_replay_preserves_bookkeeping_and_rejects_recreation(
    runtime, content_store
):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="graph-replay",
        raw_content="The deployment rule is blue approval.",
        embedding_provider=None,
    )
    first = await publish(runtime, await share_plan(runtime, source.id))
    assert first.success
    rows = await runtime.client.execute_query(
        "SELECT * FROM memory_derivations WHERE target_id=$id;",
        id=first.promoted_id,
    )
    assert len(rows) == 1 and rows[0]["active"] is True
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET revision += 1, retrieval_count += 1 WHERE uuid=$id;",
            id=source.id,
        )
    repeated = await publish(runtime, await share_plan(runtime, source.id))
    assert repeated.success and repeated.promoted_id == first.promoted_id
    assert (
        await runtime.client.execute_query(
            "SELECT * FROM memory_derivations WHERE target_id=$id;", id=first.promoted_id
        )
        == rows
    )
    await runtime.entity_manager.delete(first.promoted_id)
    import pytest

    with pytest.raises(Exception, match=r"already|index|duplicate"):
        await publish(runtime, await share_plan(runtime, source.id))
    assert not await runtime.client.execute_query(
        "SELECT * FROM entity WHERE uuid=$id;", id=first.promoted_id
    )
    retired = await runtime.client.execute_query(
        "SELECT * FROM memory_derivations WHERE target_id=$id;", id=first.promoted_id
    )
    assert len(retired) == 1 and retired[0]["active"] is False


async def test_shared_graph_validates_publisher_ancestry_without_private_reader_access(
    runtime, content_store, monkeypatch
):
    from unittest.mock import AsyncMock

    from sibyl_core.auth.memory_policy import memory_metadata_read_allowed
    from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
    from sibyl_core.services.memory_correction import apply_memory_correction
    from sibyl_core.services.memory_source_validation import SourceReadAuthority
    from tests.test_synthesis_source_observations import remember_observed_synthesis

    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="shared-ancestry",
        raw_content="Deployment requires blue approval.",
        embedding_provider=None,
    )
    synthesis = await remember_observed_synthesis(
        runtime, source.id, "raw_memory", source.revision, source.raw_content
    )
    published = await publish(runtime, await share_plan(runtime, synthesis.remembered_memory_id))
    assert published.success
    target = await runtime.entity_manager.get(published.promoted_id)
    assert memory_metadata_read_allowed(
        target.metadata,
        principal_id="user_b",
        private_scope_granted=True,
        accessible_projects={"project_a"},
        row_project_id="project_a",
    )
    resolver = AsyncMock(
        return_value=SourceReadAuthority("user_a", projects=frozenset({"project_a"}))
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.get_source_authority_resolver", lambda: resolver
    )
    assert not await unavailable_publication_ids(
        runtime.client.group_id, {target.id: target.metadata}
    )
    resolver.assert_awaited_with(runtime.client.group_id, "user_a")
    await apply_memory_correction(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id=source.id,
        action="hide",
    )
    assert target.id in await unavailable_publication_ids(
        runtime.client.group_id, {target.id: target.metadata}
    )


async def test_direct_reflection_captures_graph_before_extractor_mutates_source(
    runtime, content_store
):
    from sibyl_core.models.entities import Entity
    from sibyl_core.models.reflection import ReflectionCandidate
    from sibyl_core.tools.reflect import reflect_memory

    source = Entity(
        id="direct-source",
        entity_type=EntityType.SESSION,
        name="Source",
        content="Deployment requires blue approval.",
        metadata={"memory_scope": "private", "principal_id": "user_a"},
    )
    await runtime.entity_manager.create_direct(source)

    class RacingExtractor:
        async def extract(self, request):
            assert request.content == source.content
            await runtime.entity_manager.update(
                source.id, {"content": "Deployment now requires red approval."}
            )
            return [
                ReflectionCandidate(
                    kind="decision",
                    title="Deployment approval",
                    content=request.content,
                    reason="preserve deployment rule",
                    confidence=1.0,
                    tags=[],
                )
            ]

    pack = await reflect_memory(
        source.content,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        existing_source_id=source.id,
        persist=True,
        persist_source=False,
        extractor=RacingExtractor(),
    )
    assert pack.persisted_count == 0
    assert pack.candidates[0].persisted_id is None


async def test_direct_reflection_binds_new_graph_source_before_extraction(runtime, content_store):
    from sibyl_core.backends.surreal.records import normalize_records
    from sibyl_core.models.reflection import ReflectionCandidate
    from sibyl_core.tools.reflect import reflect_memory

    class Extractor:
        async def extract(self, request):
            rows = normalize_records(
                await runtime.client.execute_query(
                    "SELECT * FROM entity WHERE name='New reflection source'"
                )
            )
            assert len(rows) == 1
            assert rows[0]["content"] == request.content
            return [
                ReflectionCandidate(
                    kind="decision",
                    title="Deployment approval",
                    content=request.content,
                    reason="preserve deployment rule",
                    confidence=1.0,
                    tags=[],
                )
            ]

    pack = await reflect_memory(
        "Deployment requires blue approval.",
        source_title="New reflection source",
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        persist=True,
        extractor=Extractor(),
    )
    assert pack.persisted_count == 1
    rows = normalize_records(
        await runtime.client.execute_query(
            "SELECT * FROM memory_derivations WHERE target_id=$id",
            id=pack.candidates[0].persisted_id,
        )
    )
    assert rows[0]["active"] is True
    assert rows[0]["observations"][0]["source"]["id"] == pack.source_id
    assert rows[0]["observations"][0]["source"]["kind"] == "graph_entity"


async def test_graph_target_scope_change_after_discovery_retires_same_content(
    runtime, content_store, monkeypatch
):
    from unittest.mock import AsyncMock

    from sibyl_core.auth.memory_policy import memory_metadata_read_allowed
    from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="rule",
        raw_content="Deployment requires blue approval.",
        embedding_provider=None,
    )
    published = await publish(runtime, await share_plan(runtime, source.id))
    assert published.success
    target = await runtime.entity_manager.get(published.promoted_id)
    assert memory_metadata_read_allowed(
        target.metadata,
        principal_id="user_b",
        private_scope_granted=True,
        accessible_projects={"project_a"},
        row_project_id="project_a",
    )
    resolver = AsyncMock(
        return_value=SourceReadAuthority("user_a", projects=frozenset({"project_a"}))
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.get_source_authority_resolver", lambda: resolver
    )
    assert not await unavailable_publication_ids(
        runtime.client.group_id, {target.id: target.metadata}
    )
    updated = await runtime.entity_manager.update(
        target.id,
        {"metadata": {"memory_scope": "private", "principal_id": "user_a", "scope_key": None}},
    )
    assert updated.content == target.content
    assert target.id in await unavailable_publication_ids(
        runtime.client.group_id, {target.id: target.metadata}
    )


async def test_share_rejects_project_ancestor_outside_key_grant(runtime, content_store):
    from sibyl_core.auth.memory_policy import memory_scope_policy_key
    from sibyl_core.services.memory_sharing import share_memory
    from sibyl_core.services.surreal_content import MemoryScope
    from sibyl_core.services.synthesis import draft_synthesis_artifact, remember_synthesis_artifact
    from tests.test_synthesis_source_observations import materialized_synthesis

    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="project-rule",
        raw_content="Deployment requires blue approval.",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_a",
        embedding_provider=None,
    )
    run = await materialized_synthesis(
        runtime,
        source.id,
        "raw_memory",
        source.revision,
        source.raw_content,
        accessible_projects={"project_a", "project_b"},
    )
    artifact = await remember_synthesis_artifact(
        draft_synthesis_artifact(run),
        run,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )
    common = dict(
        source_ids=[artifact.remembered_memory_id],
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        target_scope="project",
        target_scope_key="project_b",
        accessible_projects={"project_a", "project_b"},
        writable_projects={"project_b"},
    )
    keys = frozenset(
        {
            memory_scope_policy_key(MemoryScope.PRIVATE, "user_a"),
            memory_scope_policy_key(MemoryScope.PROJECT, "project_b"),
        }
    )
    denied = await share_memory(**common, allowed_memory_scope_keys=keys)
    assert not denied.applied
    assert denied.preview.visible_source_ids == []
    allowed = await share_memory(
        **common,
        allowed_memory_scope_keys=keys
        | {memory_scope_policy_key(MemoryScope.PROJECT, "project_a")},
    )
    assert allowed.applied
    assert allowed.promotions[0].success


async def test_direct_reflection_source_change_during_insert_denies_publication(
    runtime, content_store, monkeypatch
):
    from sibyl_core.models.entities import Entity
    from sibyl_core.models.reflection import ReflectionCandidate
    from sibyl_core.tools.reflect import reflect_memory

    source = Entity(
        id="insert-race-source",
        entity_type=EntityType.SESSION,
        name="Source",
        content="Deployment requires blue approval.",
        metadata={"memory_scope": "private", "principal_id": "user_a"},
    )
    await runtime.entity_manager.create_direct(source)
    original_create = runtime.entity_manager.create_direct_if_absent
    inserted_ids = []

    async def create_then_revoke(entity, **kwargs):
        result = await original_create(entity, **kwargs)
        inserted_ids.append(result[0].id)
        await runtime.entity_manager.update(
            source.id, {"content": "Deployment now requires red approval."}
        )
        return result

    monkeypatch.setattr(runtime.entity_manager, "create_direct_if_absent", create_then_revoke)

    class Extractor:
        async def extract(self, request):
            return [
                ReflectionCandidate(
                    kind="decision",
                    title="Deployment approval",
                    content=request.content,
                    reason="preserve deployment rule",
                    confidence=1.0,
                    tags=[],
                )
            ]

    pack = await reflect_memory(
        source.content,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        existing_source_id=source.id,
        persist=True,
        persist_source=False,
        extractor=Extractor(),
    )
    assert pack.persisted_count == 0
    assert len(inserted_ids) == 1
    target = await runtime.entity_manager.get(inserted_ids[0])
    assert target.metadata["reflection_publication"]["state"] == "pending"


async def test_graph_descendant_retains_original_raw_ancestry(runtime, content_store, monkeypatch):
    from unittest.mock import AsyncMock

    from sibyl_core.models.reflection import ReflectionCandidate
    from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
    from sibyl_core.services.memory_correction import apply_memory_correction
    from sibyl_core.services.memory_source_validation import SourceReadAuthority
    from sibyl_core.tools.reflect import reflect_memory

    resolver = AsyncMock(
        return_value=SourceReadAuthority("user_a", projects=frozenset({"project_a"}))
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.get_source_authority_resolver", lambda: resolver
    )
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="ancestor",
        raw_content="Deployment requires blue approval.",
        embedding_provider=None,
    )
    first = await publish(runtime, await share_plan(runtime, source.id))
    assert first.success
    published = await runtime.entity_manager.get(first.promoted_id)

    class Extractor:
        async def extract(self, request):
            return [
                ReflectionCandidate(
                    kind="decision",
                    title="Descendant approval",
                    content=request.content,
                    reason="preserve approval",
                    confidence=1.0,
                    tags=[],
                )
            ]

    pack = await reflect_memory(
        published.content,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        project="project_a",
        accessible_projects={"project_a"},
        writable_projects={"project_a"},
        existing_source_id=published.id,
        persist=True,
        persist_source=False,
        extractor=Extractor(),
    )
    assert pack.persisted_count == 1
    descendant = await runtime.entity_manager.get(pack.candidates[0].persisted_id)
    rows = {descendant.id: descendant.metadata}
    assert not await unavailable_publication_ids(runtime.client.group_id, rows)
    await apply_memory_correction(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id=source.id,
        action="hide",
    )
    assert descendant.id in await unavailable_publication_ids(runtime.client.group_id, rows)
