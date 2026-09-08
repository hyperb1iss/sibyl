"""Credential grants constrain correction disclosure without suppressing writes."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import content_client
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_correction import apply_memory_correction, preview_memory_correction
from sibyl_core.services.surreal_content import get_raw_memory, remember_raw_memory


@pytest.fixture
async def correction_store(monkeypatch):
    org = str(uuid4())
    content = SurrealContentClient(url="memory://")
    graph = SurrealGraphClient(group_id=org, url="memory://")
    try:
        await bootstrap_content_schema(content, reset=True)
        await prepare_graph_schema(graph)
        runtime = GraphRuntime(
            client=graph,
            entity_manager=EntityManager(graph, group_id=org),
            relationship_manager=RelationshipManager(graph, group_id=org),
        )

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        monkeypatch.setattr(
            "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
            AsyncMock(return_value=runtime),
        )
        yield SimpleNamespace(org=org, runtime=runtime)
    finally:
        await content.close()
        await graph.close()


async def capture(store, source_id, **kwargs):
    return await remember_raw_memory(
        organization_id=store.org,
        principal_id="owner",
        source_id=source_id,
        raw_content=f"Advice from {source_id}",
        embedding_provider=None,
        **kwargs,
    )


def grants(private):
    keys = {memory_scope_policy_key("project", "project-a")}
    if private:
        keys.add(memory_scope_policy_key("private", "owner"))
    return keys


@pytest.mark.parametrize("private", [False, True])
async def test_correction_scope_grants_filter_receipts_without_skipping_writes(
    correction_store, private
):
    store = correction_store
    root = await capture(store, "root", memory_scope="project", scope_key="project-a")
    child = await capture(
        store,
        "child",
        metadata={"raw_source_ids": [root.id]},
        accessible_projects={"project-a"},
    )
    for entity_id, lineage in (
        ("direct", {"raw_memory_id": root.id}),
        ("passage", {"parent_entity_id": "direct", "projection_kind": "passage"}),
        ("indirect", {"raw_source_ids": [root.id]}),
    ):
        await store.runtime.entity_manager.create_direct(
            Entity(
                id=entity_id,
                name=entity_id,
                content="Private advice",
                entity_type=EntityType.EPISODE,
                metadata={"memory_scope": "private", "principal_id": "owner", **lineage},
            ),
            generate_embedding=False,
        )
    result = await apply_memory_correction(
        organization_id=store.org,
        source_id=root.id,
        principal_id="owner",
        action="hide",
        accessible_projects=iter(["project-a"]),
        allowed_memory_scope_keys=iter(grants(private)),
    )
    assert result.applied and result.propagation_complete
    stored = await get_raw_memory(organization_id=store.org, memory_id=child.id)
    assert stored.metadata["correction_blockers"][root.id]["blocking"] is True
    assert (child.id in result.affected_raw_memory_ids) is private
    for entity_id in ("direct", "passage", "indirect"):
        row = await store.runtime.entity_manager.get(entity_id)
        assert row.metadata["correction_blockers"][root.id]["blocking"] is True
        assert (entity_id in result.affected_entity_ids) is private


@pytest.mark.parametrize(
    "action,reference_key",
    [
        ("supersede", "replacement_source_id"),
        ("mark_duplicate", "duplicate_of_source_id"),
    ],
)
@pytest.mark.parametrize("private", [False, True])
async def test_correction_scope_grants_gate_private_references(
    correction_store, action, reference_key, private
):
    store = correction_store
    root = await capture(store, "root", memory_scope="project", scope_key="project-a")
    reference = await capture(store, "private-reference")
    result = await preview_memory_correction(
        organization_id=store.org,
        source_id=root.id,
        principal_id="owner",
        action=action,
        accessible_projects=iter(["project-a"]),
        allowed_memory_scope_keys=iter(grants(private)),
        **{reference_key: reference.id},
    )
    assert result.allowed is private
    if not private:
        assert result.reason.endswith("_source_not_found")


@pytest.mark.parametrize("private", [False, True])
async def test_correction_scope_grants_gate_caller_declared_graph_target(correction_store, private):
    store = correction_store
    root = await capture(
        store,
        "root",
        memory_scope="project",
        scope_key="project-a",
        metadata={"promoted_entity_id": "declared-private"},
    )
    await store.runtime.entity_manager.create_direct(
        Entity(
            id="declared-private",
            name="Declared private target",
            content="Private advice",
            entity_type=EntityType.EPISODE,
            metadata={"memory_scope": "private", "principal_id": "owner"},
        ),
        generate_embedding=False,
    )
    result = await apply_memory_correction(
        organization_id=store.org,
        source_id=root.id,
        principal_id="owner",
        action="hide",
        accessible_projects={"project-a"},
        allowed_memory_scope_keys=grants(private),
    )
    row = await store.runtime.entity_manager.get("declared-private")
    assert (root.id in row.metadata.get("correction_blockers", {})) is private
    assert result.refused_entity_ids == []
    assert result.propagation_complete is private
    assert result.preview.affected_derived_ids == (["declared-private"] if private else [])
    assert result.updated_memory.metadata["memory_lifecycle"]["derived_ids"] == (
        ["declared-private"] if private else []
    )
    assert result.updated_memory.metadata["promoted_entity_id"] == "declared-private"


async def test_correction_scope_grants_gate_direct_core_root(correction_store):
    store = correction_store
    root = await capture(store, "private-root")
    result = await apply_memory_correction(
        organization_id=store.org,
        source_id=root.id,
        principal_id="owner",
        action="hide",
        allowed_memory_scope_keys=grants(False),
    )
    assert not result.applied
    assert result.preview.reason == "api_key_memory_space_denied"
    stored = await get_raw_memory(organization_id=store.org, memory_id=root.id)
    assert stored.revision == root.revision


@pytest.mark.parametrize("expected_delta", [None, 0, 1])
async def test_correction_scope_authority_race_cannot_write_new_owner_snapshot(
    correction_store, monkeypatch, expected_delta
):
    from dataclasses import replace

    from sibyl_core.errors import RevisionConflictError
    from sibyl_core.services import memory_correction
    from sibyl_core.services.surreal_content import save_raw_memory

    store = correction_store
    root = await capture(store, "root")
    original_load = memory_correction._load_correction_memory
    loads = 0

    async def transfer_after_read(**kwargs):
        nonlocal loads
        row = await original_load(**kwargs)
        loads += 1
        if loads == 1:
            await save_raw_memory(
                replace(row, principal_id="new-owner"),
                expected_revision=row.observed_revision,
            )
        return row

    monkeypatch.setattr(memory_correction, "_load_correction_memory", transfer_after_read)
    with pytest.raises(RevisionConflictError):
        await apply_memory_correction(
            organization_id=store.org,
            source_id=root.id,
            principal_id="owner",
            action="hide",
            expected_revision=None if expected_delta is None else root.revision + expected_delta,
        )
    stored = await get_raw_memory(organization_id=store.org, memory_id=root.id)
    assert stored.principal_id == "new-owner"
    assert "memory_lifecycle" not in stored.metadata


async def test_correction_scope_unknown_revision_cannot_write(correction_store, monkeypatch):
    from dataclasses import replace

    from sibyl_core.services import memory_correction

    store = correction_store
    root = await capture(store, "root")
    monkeypatch.setattr(
        memory_correction,
        "_load_correction_memory",
        AsyncMock(return_value=replace(root, observed_revision=None)),
    )
    save = AsyncMock()
    monkeypatch.setattr(memory_correction, "save_raw_memory", save)
    result = await apply_memory_correction(
        organization_id=store.org,
        source_id=root.id,
        principal_id="owner",
        action="hide",
    )
    assert not result.applied
    assert result.preview.reason == "memory_source_revision_unavailable"
    save.assert_not_awaited()


async def test_correction_scope_readable_refusal_remains_visible(correction_store):
    store = correction_store
    root = await capture(
        store,
        "root",
        memory_scope="project",
        scope_key="project-a",
        metadata={"promoted_entity_id": "project-a"},
    )
    await store.runtime.entity_manager.create_direct(
        Entity(id="project-a", name="Project A", entity_type=EntityType.PROJECT),
        generate_embedding=False,
    )
    result = await apply_memory_correction(
        organization_id=store.org,
        source_id=root.id,
        principal_id="owner",
        action="hide",
        accessible_projects={"project-a"},
        allowed_memory_scope_keys=grants(False),
    )
    assert result.applied and not result.propagation_complete
    assert result.preview.affected_derived_ids == ["project-a"]
    assert result.refused_entity_ids == ["project-a"]
    row = await store.runtime.entity_manager.get("project-a")
    assert "correction_blockers" not in row.metadata


@pytest.mark.parametrize("write_granted", [False, True])
async def test_correction_write_projects_are_distinct_from_reference_visibility(
    correction_store, write_granted
):
    store = correction_store
    root = await capture(
        store,
        "role-root",
        memory_scope="project",
        scope_key="project-a",
        metadata={"promoted_entity_id": "project-b"},
    )
    await store.runtime.entity_manager.create_direct(
        Entity(id="project-b", name="Readable project", entity_type=EntityType.PROJECT),
        generate_embedding=False,
    )
    kwargs = {
        "organization_id": store.org,
        "source_id": root.id,
        "principal_id": "owner",
        "action": "hide",
        "accessible_projects": {"project-a", "project-b"},
        "writable_projects": {"project-a"} if write_granted else set(),
    }
    preview = await preview_memory_correction(**kwargs)
    assert preview.allowed is write_granted
    if write_granted:
        assert preview.affected_derived_ids == ["project-b"]
    applied = await apply_memory_correction(**kwargs)
    assert applied.applied is write_granted
    stored = await get_raw_memory(organization_id=store.org, memory_id=root.id)
    assert stored is not None
    assert (stored.revision > root.revision) is write_granted


async def test_correction_reloaded_root_cannot_use_another_projects_write_grant(correction_store):
    store = correction_store
    root = await capture(store, "moved-root", memory_scope="project", scope_key="read-only")
    result = await apply_memory_correction(
        organization_id=store.org,
        source_id=root.id,
        principal_id="owner",
        action="delete",
        accessible_projects={"original-writable", "read-only"},
        writable_projects={"original-writable"},
    )
    assert not result.applied
    stored = await get_raw_memory(organization_id=store.org, memory_id=root.id)
    assert stored is not None
    assert stored.raw_content == root.raw_content
    assert stored.revision == root.revision


@pytest.mark.parametrize("target_writable", [False, True])
async def test_declared_project_target_requires_its_own_write_grant(
    correction_store, target_writable
):
    store = correction_store
    root = await capture(
        store, "declared-role-root", metadata={"promoted_entity_id": "declared-project-row"}
    )
    await store.runtime.entity_manager.create_direct(
        Entity(
            id="declared-project-row",
            name="Project row",
            entity_type=EntityType.EPISODE,
            metadata={"memory_scope": "project", "scope_key": "project-b"},
        ),
        generate_embedding=False,
    )
    result = await apply_memory_correction(
        organization_id=store.org,
        source_id=root.id,
        principal_id="owner",
        action="hide",
        accessible_projects={"project-b"},
        writable_projects={"project-b"} if target_writable else set(),
    )
    assert result.applied
    target = await store.runtime.entity_manager.get("declared-project-row")
    assert ("correction_blockers" in target.metadata) is target_writable
    assert result.refused_entity_ids == ([] if target_writable else [target.id])
