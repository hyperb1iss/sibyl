"""Correction failure and ordering checks from independent fault probes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import memory_lifecycle as life
from sibyl_core.services import memory_lineage as lineage
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.memory_contract import MemoryCorrectionPreview


def preview(action):
    return MemoryCorrectionPreview(
        allowed=True,
        source_id="root",
        action=action,
        reason="test",
        target_lifecycle_state="active",
        target_lifecycle_flags=[],
        affected_source_ids=["root"],
        affected_derived_ids=[],
        reversible=True,
        recall_impact={},
        synthesis_impact={},
        audit_action="test",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lookup", "runtime"])
async def test_initial_graph_failure_keeps_raw_propagation(monkeypatch, failure):
    root = RawMemory(
        id="root",
        organization_id="org",
        source_id="root",
        principal_id="owner",
        revision=2,
        observed_revision=2,
        metadata={"lifecycle_flags": ["hidden"]},
    )
    child = RawMemory(
        id="child",
        organization_id="org",
        source_id="child",
        principal_id="owner",
        revision=1,
        observed_revision=1,
        metadata={"raw_source_ids": ["root"]},
    )

    async def raw_children(*, source_ids, **kwargs):
        if "root" in source_ids:
            yield "child"

    save = AsyncMock(return_value=child)
    runtime = SimpleNamespace(
        client=SimpleNamespace(
            execute_query=AsyncMock(side_effect=RuntimeError("graph lookup unavailable"))
        )
    )
    monkeypatch.setattr(
        life,
        "get_surreal_graph_runtime",
        AsyncMock(
            side_effect=RuntimeError("runtime unavailable") if failure == "runtime" else None,
            return_value=runtime,
        ),
    )
    monkeypatch.setattr(lineage, "_raw_descendant_ids", raw_children)
    monkeypatch.setattr(lineage, "get_raw_memory", AsyncMock(return_value=child))
    monkeypatch.setattr(lineage, "save_raw_memory", save)
    result = await life._project_correction_to_graph(
        organization_id="org",
        memory=root,
        preview=preview("hide"),
        principal_id="owner",
        accessible_projects=set(),
        replacement_source_id=None,
        duplicate_of_source_id=None,
    )
    assert not result[3].complete
    assert save.await_count == 1, (
        "Healthy raw descendants were never attempted after graph lookup failure"
    )


@pytest.mark.asyncio
async def test_failed_edge_unlink_is_incomplete(monkeypatch):
    root = RawMemory(
        id="root",
        organization_id="org",
        source_id="root",
        principal_id="owner",
        revision=3,
        observed_revision=3,
    )
    graph = Entity(
        id="copy",
        name="copy",
        entity_type=EntityType.EPISODE,
        observed_revision=1,
        metadata={"memory_scope": "private", "principal_id": "owner"},
    )

    async def execute(query, **kwargs):
        if query.strip().startswith("DELETE"):
            raise RuntimeError("edge deletion unavailable")
        return []

    runtime = SimpleNamespace(
        client=SimpleNamespace(execute_query=execute),
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=graph), update=AsyncMock(return_value=graph)
        ),
    )

    async def raw_children(**kwargs):
        if False:
            yield ""

    monkeypatch.setattr(life, "get_surreal_graph_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(
        life,
        "_correction_graph_entity_ids",
        AsyncMock(
            return_value=life._CorrectionGraphTargets(
                authorized=["copy"], refused=[], direct=["copy"]
            )
        ),
    )
    monkeypatch.setattr(lineage, "_raw_descendant_ids", raw_children)
    result = await life._project_correction_to_graph(
        organization_id="org",
        memory=root,
        preview=preview("restore"),
        principal_id="owner",
        accessible_projects=set(),
        replacement_source_id=None,
        duplicate_of_source_id=None,
    )
    assert not result[3].complete, (
        "Restore claims complete although its correction edge still excludes the row"
    )


@pytest.mark.asyncio
async def test_stale_supersede_does_not_recreate_restored_edge(monkeypatch):
    root = RawMemory(
        id="root",
        organization_id="org",
        source_id="root",
        principal_id="owner",
        revision=2,
        observed_revision=2,
        metadata={"lifecycle_state": "superseded"},
    )
    replacement = RawMemory(
        id="replacement",
        organization_id="org",
        source_id="replacement",
        principal_id="owner",
        revision=1,
        observed_revision=1,
    )
    # The newer restore already cleared this root before the supersede worker resumed.
    copy = Entity(
        id="copy",
        name="copy",
        entity_type=EntityType.EPISODE,
        observed_revision=3,
        metadata={
            "memory_scope": "private",
            "principal_id": "owner",
            "correction_blockers": {"root": {"revision": 3, "blocking": False}},
        },
    )
    new = Entity(
        id="new",
        name="new",
        entity_type=EntityType.EPISODE,
        observed_revision=1,
        metadata={"memory_scope": "private", "principal_id": "owner"},
    )

    async def raw_children(**kwargs):
        if False:
            yield ""

    async def targets(_runtime, *, memory, **kwargs):
        ids = ["copy"] if memory.id == "root" else ["new"]
        return life._CorrectionGraphTargets(authorized=ids, refused=[], direct=ids)

    edges = []

    async def create(edge):
        edges.append(edge)
        return edge.id

    runtime = SimpleNamespace(
        client=SimpleNamespace(execute_query=AsyncMock(return_value=[])),
        entity_manager=SimpleNamespace(
            get=AsyncMock(side_effect=lambda entity_id: copy if entity_id == "copy" else new),
            update=AsyncMock(),
        ),
        relationship_manager=SimpleNamespace(create=create),
    )
    monkeypatch.setattr(life, "get_surreal_graph_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(life, "_correction_graph_entity_ids", targets)
    monkeypatch.setattr(life, "get_raw_memory_by_source_id", AsyncMock(return_value=replacement))
    monkeypatch.setattr(lineage, "_raw_descendant_ids", raw_children)
    result = await life._project_correction_to_graph(
        organization_id="org",
        memory=root,
        preview=preview("supersede"),
        principal_id="owner",
        accessible_projects=set(),
        replacement_source_id="replacement",
        duplicate_of_source_id=None,
    )
    assert result[3].complete
    runtime.entity_manager.update.assert_not_awaited()
    assert edges == [], "Stale supersede recreates an inbound exclusion after a newer restore"
