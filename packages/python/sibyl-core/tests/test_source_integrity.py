"""Restore loss must not recreate the identity of previously observed evidence."""

from dataclasses import replace
from uuid import UUID

import pytest

from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.source_state_store import load_source_snapshot
from tests.test_synthesis_source_observations import (
    content_store as content_store,
)
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)
from tests.test_synthesis_source_observations import (
    runtime as runtime,
)


async def snapshot(runtime, identity):
    source = SourceIdentity(runtime.client.group_id, SourceKind.GRAPH_ENTITY, identity)
    return await load_source_snapshot(
        source, organization_id=source.organization_id, execute_query=runtime.client.execute_query
    )


async def seed(runtime, identity):
    await runtime.entity_manager.create_direct(
        Entity(id=identity, entity_type=EntityType.SESSION, name="Evidence", content="Blue")
    )
    return await snapshot(runtime, identity)


@pytest.mark.parametrize("version", [23, 25])
async def test_integrity_upgrade_missing_state_never_receives_legacy_identity(runtime, version):
    first = await seed(runtime, "missing")
    old = replace(first.observation, incarnation=None)
    await runtime.client.execute_query(
        "UPDATE source_states UNSET incarnation; REMOVE FIELD incarnation ON source_states; "
        "DELETE source_states WHERE source_id='missing'; "
        "UPDATE schema_version SET version=$version WHERE name='graph';",
        version=version,
    )
    await bootstrap_schema(runtime.client)
    await runtime.client.execute_query(
        "UPDATE entity SET retrieval_count += 1 WHERE uuid='missing';"
    )
    repaired = await snapshot(runtime, "missing")
    assert UUID(repaired.observation.incarnation)
    assert not old.same_evidence(repaired.observation)
    entity = await runtime.entity_manager.get("missing")
    await runtime.entity_manager.delete(entity.id)
    await runtime.entity_manager.create_direct(entity)
    assert not old.same_evidence((await snapshot(runtime, entity.id)).observation)


async def test_integrity_upgrade_keeps_valid_and_stale_old_observations(runtime):
    valid = replace((await seed(runtime, "valid")).observation, incarnation=None)
    stale = replace((await seed(runtime, "stale")).observation, incarnation=None)
    await runtime.entity_manager.update("stale", {"content": "Green"})
    await runtime.client.execute_query(
        "UPDATE source_states UNSET incarnation; REMOVE FIELD incarnation ON source_states; "
        "UPDATE schema_version SET version=25 WHERE name='graph';"
    )
    await bootstrap_schema(runtime.client)
    assert valid.same_evidence((await snapshot(runtime, "valid")).observation)
    assert not stale.same_evidence((await snapshot(runtime, "stale")).observation)


async def test_lost_ledger_bookkeeping_cannot_revive_old_incarnation(runtime):
    first = await seed(runtime, "lost")
    entity = await runtime.entity_manager.get("lost")
    await runtime.entity_manager.delete(entity.id)
    await runtime.entity_manager.create_direct(entity)
    await runtime.client.execute_query("DELETE source_states WHERE source_id='lost';")
    assert await snapshot(runtime, entity.id) is None
    await runtime.client.execute_query("UPDATE entity SET retrieval_count += 1 WHERE uuid='lost';")
    repaired = await snapshot(runtime, entity.id)
    assert repaired.observation.generation == first.observation.generation
    assert repaired.observation.incarnation != first.observation.incarnation
    assert not repaired.observation.same_evidence(first.observation)


async def test_graph_protected_marker_survives_association_loss(runtime, content_store):
    from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
    from sibyl_core.services.surreal_content import remember_raw_memory
    from tests.test_graph_derivation_publication import publish, share_plan

    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="marker",
        raw_content="Deployment requires blue approval.",
        embedding_provider=None,
    )
    result = await publish(runtime, await share_plan(runtime, raw.id))
    assert result.success
    target = await runtime.entity_manager.get(result.promoted_id)
    assert target.derivation_required
    before = await snapshot(runtime, target.id)
    await runtime.client.execute_query(
        "UPDATE entity SET retrieval_count += 1 WHERE uuid=$id;", id=target.id
    )
    assert before.observation.same_evidence((await snapshot(runtime, target.id)).observation)
    await runtime.client.execute_query(
        "DELETE memory_derivations WHERE target_id=$id;", id=target.id
    )
    assert target.id in await unavailable_publication_ids(
        runtime.client.group_id, {target.id: target.metadata}
    )
    for value in (False, None):
        with pytest.raises(Exception, match="cannot lose lineage"):
            await runtime.client.execute_query(
                "UPDATE entity SET derivation_required=$value WHERE uuid=$id;",
                id=target.id,
                value=value,
            )


async def test_raw_protected_marker_denies_missing_association_after_marker_metadata_edit(
    runtime, content_store
):
    from sibyl_core.services.memory_derivations import raw_derivation_current
    from sibyl_core.services.memory_source_validation import SourceReadAuthority
    from sibyl_core.services.surreal_content import get_raw_memory, remember_raw_memory
    from tests.test_synthesis_source_observations import remember_observed_synthesis

    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="raw-marker",
        raw_content="Deployment requires blue approval.",
        embedding_provider=None,
    )
    result = await remember_observed_synthesis(
        runtime, raw.id, "raw_memory", raw.revision, raw.raw_content
    )
    memory = await get_raw_memory(
        organization_id=runtime.client.group_id, memory_id=result.remembered_memory_id
    )
    assert memory.derivation_required
    from sibyl_core.services import content_client

    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "DELETE memory_derivations WHERE target_id=$id; "
            "UPDATE raw_captures SET capture_surface='ordinary', metadata={} WHERE uuid=$id;",
            id=memory.id,
        )
    memory = await get_raw_memory(organization_id=runtime.client.group_id, memory_id=memory.id)
    assert memory.derivation_required
    assert not await raw_derivation_current(
        memory, SourceReadAuthority("user_a", projects=frozenset({"project_a"}))
    )


async def test_content_upgrade_from_33_preserves_retained_identity(runtime, content_store):
    from sibyl_core.backends.surreal.content_schema import (
        CONTENT_SCHEMA_CURRENT_VERSION,
        bootstrap_content_schema,
    )
    from sibyl_core.services import content_client
    from sibyl_core.services.surreal_content import remember_raw_memory

    memory = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="content-upgrade",
        raw_content="Retained source evidence",
        embedding_provider=None,
    )
    source = SourceIdentity(runtime.client.group_id, SourceKind.RAW_CAPTURE, memory.id)
    async with content_client.surreal_content_client() as client:
        before = await load_source_snapshot(
            source, organization_id=source.organization_id, execute_query=client.execute_query
        )
        legacy = replace(before.observation, incarnation=None)
        await client.execute_query(
            "UPDATE source_states UNSET incarnation; REMOVE FIELD incarnation ON source_states; "
            "UPDATE schema_version SET version=33 WHERE name='content';"
        )
        await bootstrap_content_schema(client)
        current = (
            await client.execute_query("SELECT version FROM schema_version WHERE name='content';")
        )[0]["version"]
        assert current == CONTENT_SCHEMA_CURRENT_VERSION
        after = await load_source_snapshot(
            source, organization_id=source.organization_id, execute_query=client.execute_query
        )
        assert legacy.same_evidence(after.observation)


async def test_graph_upgrade_allows_existing_ordinary_row_update(runtime):
    before = await seed(runtime, "ordinary-upgrade")
    await runtime.client.execute_query(
        "UPDATE entity UNSET derivation_required; REMOVE FIELD derivation_required ON entity; "
        "UPDATE schema_version SET version=25 WHERE name='graph';"
    )
    await bootstrap_schema(runtime.client)
    await runtime.entity_manager.update("ordinary-upgrade", {"name": "Updated evidence"})
    after = await snapshot(runtime, "ordinary-upgrade")
    assert after.entity.name == "Updated evidence"
    assert not after.entity.derivation_required
    assert after.observation.incarnation == before.observation.incarnation
    assert after.observation.generation > before.observation.generation
