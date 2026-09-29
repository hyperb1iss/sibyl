# Unbound projections retain source history without read-time repair.

from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_capture_availability import available_capture_projection_rows
from sibyl_core.services.memory_correction import apply_memory_correction
from tests.test_capture_corrections import captured_note
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


def projection(identifier, parent):
    return Entity(
        id=identifier,
        entity_type=EntityType.PASSAGE,
        name="Legacy passage",
        content="Original source advice",
        metadata={"projection_kind": "passage", "parent_entity_id": parent},
    )


@pytest.mark.parametrize("action", ["hide", "revise"])
@pytest.mark.parametrize("support", ["graph_parent", "declared_capture"])
async def test_unbound_graph_descendant_keeps_unknown_content_epoch_after_restore(
    runtime, content_store, monkeypatch, action, support
):
    memory, parent = await captured_note(runtime, monkeypatch)
    child = projection("legacy-child", parent.id)
    if support == "declared_capture":
        child.metadata = {"projection_kind": "passage", "raw_source_ids": [memory.id]}
    original = child.model_dump()
    rows = {child.id: child}
    assert set(
        await available_capture_projection_rows(
            runtime.client.group_id, rows, graph_client=runtime.client
        )
    ) == {child.id}
    monkeypatch.setattr(
        runtime.entity_manager,
        "update",
        AsyncMock(side_effect=RuntimeError("graph stamp unavailable")),
    )
    result = await apply_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action=action,
        **({"revised_content": "New source advice"} if action == "revise" else {}),
    )
    assert result.applied and not result.propagation_complete
    assert not await available_capture_projection_rows(
        runtime.client.group_id, rows, graph_client=runtime.client
    )
    restored = await apply_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action="restore",
    )
    assert restored.applied
    available = await available_capture_projection_rows(
        runtime.client.group_id, rows, graph_client=runtime.client
    )
    assert (child.id in available) is (action == "hide")
    assert child.model_dump() == original
    assert "source_bindings" not in child.metadata


@pytest.mark.parametrize("pointer", [None, 123, ["parent"], ""])
async def test_malformed_graph_parent_excludes_only_dependent_projection(
    runtime, content_store, pointer
):
    child = projection("bad-child", pointer)
    unrelated = Entity(id="ordinary-org-resource", name="Resource", entity_type=EntityType.NOTE)
    assert set(
        await available_capture_projection_rows(
            runtime.client.group_id,
            {child.id: child, unrelated.id: unrelated},
            graph_client=runtime.client,
        )
    ) == {unrelated.id}


async def test_missing_foreign_and_cyclic_graph_ancestry_fail_closed(runtime, content_store):
    from sibyl_core.services.graph_entity_store import _entity_record

    missing = projection("missing-child", "absent-parent")
    foreign = projection("foreign-child", "foreign-parent")
    foreign_parent = Entity(
        id="foreign-parent",
        name="Other org",
        entity_type=EntityType.NOTE,
        organization_id="different-org",
    )
    row = _entity_record(foreign_parent, group_id="different-org")
    await runtime.client.execute_query("INSERT INTO entity $row;", row=row)
    cycle_a = projection("cycle-a", "cycle-b")
    cycle_b = projection("cycle-b", "cycle-a")
    for child in (cycle_a, cycle_b):
        await runtime.entity_manager.create_direct(child, generate_embedding=False)
    unrelated = Entity(id="ordinary-org-resource", name="Resource", entity_type=EntityType.NOTE)
    rows = {row.id: row for row in (missing, foreign, cycle_a, unrelated)}
    assert set(
        await available_capture_projection_rows(
            runtime.client.group_id, rows, graph_client=runtime.client
        )
    ) == {unrelated.id}


async def test_graph_parent_reads_are_batched_and_never_write(runtime, content_store, monkeypatch):
    from sibyl_core.services import content_client

    _memory, parent = await captured_note(runtime, monkeypatch)
    children = {f"child-{i}": projection(f"child-{i}", parent.id) for i in range(100)}
    graph_query = AsyncMock(wraps=runtime.client.execute_query)
    monkeypatch.setattr(runtime.client, "execute_query", graph_query)
    async with content_client.surreal_content_client() as client:
        content_query = AsyncMock(wraps=client.execute_query)
        monkeypatch.setattr(client, "execute_query", content_query)
    assert set(
        await available_capture_projection_rows(
            runtime.client.group_id, children, graph_client=runtime.client
        )
    ) == set(children)
    assert graph_query.await_count == 2
    assert content_query.await_count == 1
    for call in [*graph_query.await_args_list, *content_query.await_args_list]:
        assert call.args[0].lstrip().upper().startswith(("SELECT ", "RETURN "))
    assert all("source_bindings" not in child.metadata for child in children.values())


async def test_failed_graph_lookup_does_not_remove_ordinary_org_rows(
    runtime, content_store, monkeypatch
):
    _memory, parent = await captured_note(runtime, monkeypatch)
    child = projection("dependent-child", parent.id)
    unrelated = Entity(id="ordinary-org-resource", name="Resource", entity_type=EntityType.NOTE)
    monkeypatch.setattr(
        runtime.client,
        "execute_query",
        AsyncMock(side_effect=RuntimeError("graph lookup unavailable")),
    )
    assert set(
        await available_capture_projection_rows(
            runtime.client.group_id,
            {child.id: child, unrelated.id: unrelated},
            graph_client=runtime.client,
        )
    ) == {unrelated.id}


@pytest.mark.parametrize("mode", ["raw", "share"])
async def test_authorized_bound_publication_remains_readable_to_project_member(
    runtime, content_store, monkeypatch, mode
):
    from sibyl_core.auth.memory_policy import memory_metadata_read_allowed
    from sibyl_core.services.memory_reflection import promote_raw_memory
    from sibyl_core.services.memory_sharing import share_memory

    memory, _parent = await captured_note(runtime, monkeypatch)

    async def source_authority(organization_id, principal_id):
        from sibyl_core.services.memory_source_validation import SourceReadAuthority

        if organization_id != runtime.client.group_id or principal_id != "user_a":
            return None
        return SourceReadAuthority(principal_id, projects=frozenset({"project_a"}))

    monkeypatch.setattr("sibyl_core.runtime_ports._source_authority_resolver", source_authority)
    common = dict(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        accessible_projects={"project_a"},
        writable_projects={"project_a"},
    )
    if mode == "raw":
        result = await promote_raw_memory(
            raw_memory_id=memory.id,
            promote_to_scope="project",
            promote_to_scope_key="project_a",
            **common,
        )
    else:
        shared = await share_memory(
            source_ids=[memory.id],
            target_scope="project",
            target_scope_key="project_a",
            **common,
        )
        assert len(shared.promotions) == 1
        result = shared.promotions[0]
    assert result.success
    published = await runtime.entity_manager.get(result.promoted_id)
    assert published.metadata["source_bindings"]

    def visible(row):
        return memory_metadata_read_allowed(
            row.metadata,
            principal_id="user_b",
            private_scope_granted=True,
            accessible_projects={"project_a"},
        )

    assert memory.memory_scope == "private"
    assert memory.principal_id == "user_a"
    assert visible(published)
    assert published.id in await available_capture_projection_rows(
        runtime.client.group_id,
        {published.id: published},
        graph_client=runtime.client,
        source_visible=visible,
    )
    for action, extra in (("revise", {"revised_content": "New source advice"}), ("restore", {})):
        correction = await apply_memory_correction(
            organization_id=runtime.client.group_id,
            source_id=memory.id,
            principal_id="user_a",
            action=action,
            **extra,
        )
        assert correction.applied
        assert published.id not in await available_capture_projection_rows(
            runtime.client.group_id,
            {published.id: published},
            graph_client=runtime.client,
            source_visible=visible,
        )


async def test_projection_type_keeps_stored_parent_identity_when_marker_is_absent(
    runtime, content_store, monkeypatch
):
    memory, parent = await captured_note(runtime, monkeypatch)
    child = projection("legacy-unmarked", parent.id)
    child.metadata = {"source_entity_id": parent.id}
    assert child.id in await available_capture_projection_rows(
        runtime.client.group_id,
        {child.id: child},
        graph_client=runtime.client,
    )
    result = await apply_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action="hide",
    )
    assert result.applied
    assert child.id not in await available_capture_projection_rows(
        runtime.client.group_id,
        {child.id: child},
        graph_client=runtime.client,
    )


@pytest.mark.parametrize("bindings", [False, [], "", {"capture": True}])
async def test_malformed_retained_bindings_exclude_only_dependent_rows(
    runtime, content_store, bindings
):
    unrelated = Entity(id="ordinary-org-resource", name="Resource", entity_type=EntityType.NOTE)
    await runtime.entity_manager.create_direct(unrelated, generate_embedding=False)
    unrelated = await runtime.entity_manager.get(unrelated.id)
    child = projection("malformed-bindings", unrelated.id)
    child.metadata["source_bindings"] = bindings
    assert set(
        await available_capture_projection_rows(
            runtime.client.group_id,
            {child.id: child, unrelated.id: unrelated},
            graph_client=runtime.client,
        )
    ) == {unrelated.id}
