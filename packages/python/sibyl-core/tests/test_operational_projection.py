"""Retained operational evidence controls actual graph publication and recall."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.models.experience import OperationalExperience
from sibyl_core.services.content_raw_persistence import remember_raw_memory
from sibyl_core.services.graph_derivations import (
    graph_target_digest,
    unavailable_graph_derivation_ids,
)
from sibyl_core.services.graph_records import entity_from_surreal_row
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.operational_capture import OperationalSourceWrite, canonical_experience
from sibyl_core.services.operational_projection import load_operational_projection_source
from sibyl_core.services.source_observations import SourceUnavailableError
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


@pytest.fixture
def authority(monkeypatch):
    value = SourceReadAuthority("user_a", projects=frozenset({"project_a"}))
    monkeypatch.setattr(
        "sibyl_core.services.operational_projection.get_source_authority_resolver",
        lambda: AsyncMock(return_value=value),
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.get_source_authority_resolver",
        lambda: AsyncMock(return_value=value),
    )
    return value


async def capture(runtime, authority, *, outcome="failed", revision=None, metadata=None):
    org = runtime.client.group_id
    payload = OperationalExperience.model_validate(
        {
            "source_id": "operation",
            "project_id": "project_a",
            "goal": "inspect",
            "outcome": outcome,
            "metadata": metadata or {},
            "observations": [
                {
                    "id": "state",
                    "ordinal": 0,
                    "evidence": [{"id": "text", "content": "The displayed page is unchanged."}],
                }
            ],
        }
    )
    memory = await remember_raw_memory(
        organization_id=org,
        principal_id="user_a",
        source_id="operation",
        raw_content=canonical_experience(payload),
        memory_scope="project",
        scope_key="project_a",
        metadata={"project_id": "project_a"},
        capture_surface="operational_experience",
        embedding_provider=None,
        operational_write=OperationalSourceWrite(org, "operation", "user_a", "project_a", revision),
    )
    source = await load_operational_projection_source(
        SourceIdentity(org, SourceKind.RAW_CAPTURE, memory.id), authority
    )
    return memory, source


async def test_operational_projection_replay_retains_exact_graph_state(
    runtime, content_store, authority
):
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    ids = list(projection.manifest.entity_ids)
    before = await runtime.client.execute_query("SELECT * FROM entity WHERE uuid IN $ids;", ids=ids)
    assert len(before) == len(ids)
    assert all(row["derivation_required"] for row in before)
    assert not await unavailable_graph_derivation_ids(runtime.client.group_id, ids)
    associations = await runtime.client.execute_query("SELECT * FROM memory_derivations;")
    assert {row["target_id"] for row in associations} == set(ids), associations
    for row in before:
        association = next(a for a in associations if a["target_id"] == row["uuid"])
        assert row["group_id"] == runtime.client.group_id
        assert association["active"] is True
        assert association["organization_id"] == runtime.client.group_id
        assert association["target_kind"] == "graph_entity"
        assert association["body_sha256"] == graph_target_digest(entity_from_surreal_row(row))
        assert len(association["observations"]) == 1
    await runtime.entity_manager.publish_operational_entities(source)
    assert before == await runtime.client.execute_query(
        "SELECT * FROM entity WHERE uuid IN $ids;", ids=ids
    )


async def test_operational_projection_old_source_cannot_overwrite_new(
    runtime, content_store, authority
):
    first, old = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(old)
    _, new = await capture(runtime, authority, outcome="unknown", revision=first.revision)
    ids = list(projection.manifest.entity_ids)
    assert await unavailable_graph_derivation_ids(runtime.client.group_id, ids) == set(ids)
    changed = await runtime.entity_manager.publish_operational_entities(new)
    assert not await unavailable_graph_derivation_ids(
        runtime.client.group_id, list(changed.manifest.entity_ids)
    )
    removed = set(ids) - set(changed.manifest.entity_ids)
    assert await unavailable_graph_derivation_ids(runtime.client.group_id, list(removed)) == removed
    with pytest.raises(SourceUnavailableError):
        await runtime.entity_manager.publish_operational_entities(old)


async def test_operational_projection_rejects_forged_scope(runtime, content_store, authority):
    _, source = await capture(runtime, authority)
    with pytest.raises(SourceUnavailableError):
        await runtime.entity_manager.publish_operational_entities(
            replace(source, project_id="project_b")
        )


async def test_operational_projection_missing_association_denies_recall_and_replay(
    runtime, content_store, authority
):
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    identifier = projection.manifest.entity_ids[0]
    await runtime.client.execute_query(
        "DELETE memory_derivations WHERE target_id=$id;", id=identifier
    )
    assert await unavailable_graph_derivation_ids(runtime.client.group_id, [identifier]) == {
        identifier
    }
    with pytest.raises(SourceUnavailableError):
        await runtime.entity_manager.publish_operational_entities(source)


async def test_operational_projection_final_target_change_rolls_back(
    runtime, content_store, authority, monkeypatch
):
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    identifier = projection.manifest.entity_ids[0]
    execute = runtime.client.execute_query
    changed = False

    async def race(query, **kwargs):
        nonlocal changed
        if "FOR $entry IN $entries" in query and not changed:
            changed = True
            await execute(
                "UPDATE entity SET content='concurrent replacement' WHERE uuid=$id;", id=identifier
            )
        return await execute(query, **kwargs)

    monkeypatch.setattr(runtime.client, "execute_query", race)
    with pytest.raises(Exception, match="operational graph target changed"):
        await runtime.entity_manager.publish_operational_entities(source)
    assert changed
    row = await runtime.entity_manager.get(identifier)
    assert row.content == "concurrent replacement"


async def test_operational_projection_retirement_after_graph_commit_is_unrecallable(
    runtime, content_store, authority, monkeypatch
):
    from sibyl_core.services import content_client

    memory, source = await capture(runtime, authority)
    execute = runtime.client.execute_query
    retired = False

    async def race(query, **kwargs):
        nonlocal retired
        result = await execute(query, **kwargs)
        if "FOR $entry IN $entries" in query and not retired:
            retired = True
            async with content_client.surreal_content_client() as client:
                await client.execute_query("DELETE raw_captures WHERE uuid=$id;", id=memory.id)
        return result

    monkeypatch.setattr(runtime.client, "execute_query", race)
    with pytest.raises(SourceUnavailableError):
        await runtime.entity_manager.publish_operational_entities(source)
    rows = await execute("SELECT uuid FROM entity WHERE derivation_required=true;")
    ids = [row["uuid"] for row in rows]
    assert retired and ids
    assert await unavailable_graph_derivation_ids(runtime.client.group_id, ids) == set(ids)


async def test_operational_projection_changed_parent_advances_unchanged_child_generation(
    runtime, content_store, authority
):
    first, source = await capture(runtime, authority, outcome="failed")
    old = await runtime.entity_manager.publish_operational_entities(source)
    before = await runtime.client.execute_query(
        "SELECT * FROM source_states WHERE source_kind='graph_entity';"
    )
    old_states = {row["source_id"]: row for row in before}
    _, changed = await capture(
        runtime, authority, revision=first.revision, metadata={"encoding_revision": 2}
    )
    new = await runtime.entity_manager.publish_operational_entities(changed)
    equal = {
        entity.id
        for entity in new.entities
        if any(
            previous.id == entity.id
            and graph_target_digest(previous) == graph_target_digest(entity)
            for previous in old.entities
        )
    }
    assert equal
    after = await runtime.client.execute_query(
        "SELECT * FROM source_states WHERE source_kind='graph_entity';"
    )
    for state in after:
        if state["source_id"] in equal:
            assert state["generation"] > old_states[state["source_id"]]["generation"]


async def test_operational_projection_revoked_read_membership_denies_publish_and_recall(
    runtime, content_store, authority, monkeypatch
):
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    denied = SourceReadAuthority("user_a")
    for module in ("operational_projection", "graph_derivations"):
        monkeypatch.setattr(
            f"sibyl_core.services.{module}.get_source_authority_resolver",
            lambda: AsyncMock(return_value=denied),
        )
    with pytest.raises(SourceUnavailableError):
        await runtime.entity_manager.publish_operational_entities(source)
    ids = list(projection.manifest.entity_ids)
    assert await unavailable_graph_derivation_ids(runtime.client.group_id, ids) == set(ids)


async def test_operational_projection_unbound_target_collision_writes_nothing(
    runtime, content_store, authority
):
    _, source = await capture(runtime, authority)
    projection = await source.projection()
    collision = projection.entities[0].model_copy(update={"content": "unrelated original"})
    await runtime.entity_manager.create_direct(collision)
    before = await runtime.client.execute_query("SELECT * FROM entity ORDER BY uuid;")
    with pytest.raises(SourceUnavailableError):
        await runtime.entity_manager.publish_operational_entities(source)
    assert before == await runtime.client.execute_query("SELECT * FROM entity ORDER BY uuid;")
    assert not await runtime.client.execute_query("SELECT * FROM memory_derivations;")


async def test_operational_projection_cannot_adopt_changed_creator(
    runtime, content_store, authority
):
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    identifier = projection.manifest.entity_ids[0]
    await runtime.client.execute_query(
        "UPDATE entity SET created_by='other' WHERE uuid=$id;", id=identifier
    )
    with pytest.raises(SourceUnavailableError):
        await runtime.entity_manager.publish_operational_entities(source)
