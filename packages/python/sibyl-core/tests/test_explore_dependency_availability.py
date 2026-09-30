"""Public dependency readers follow only current persisted graph paths."""

from contextlib import asynccontextmanager
from importlib import import_module
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import graph_view_availability
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_synthesis_source_observations import content_store as content_store
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)
from tests.test_synthesis_source_observations import runtime as runtime

exploration = import_module("sibyl_core.tools.explore")


@asynccontextmanager
async def dependency_graph():
    """Provide ordinary graph fixtures without substituting availability verdicts."""
    client = SurrealGraphClient(group_id=f"dependency-{uuid4().hex}", url="memory://")
    try:
        await prepare_graph_schema(client)
        yield GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=client.group_id),
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
    finally:
        await client.close()


async def store_dependency_graph(runtime, entities, pairs, *, kind=RelationshipType.DEPENDS_ON):
    await runtime.entity_manager.create_direct_bulk(entities)
    edges = [
        Relationship(
            id=f"edge-{source}-{target}",
            source_id=source,
            target_id=target,
            relationship_type=kind,
        )
        for source, target in pairs
    ]
    await runtime.relationship_manager.create_direct_bulk(edges)
    return edges


def task(task_id, **kwargs):
    return Entity(id=task_id, entity_type=EntityType.TASK, name=task_id, **kwargs)


async def source_task(runtime, *, scope="private"):
    key = {"project": "project_a", "team": "team_a", "delegated": "space_a"}.get(scope)
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="dependency-source",
        raw_content="Retained dependency evidence",
        memory_scope=scope,
        scope_key=key,
        embedding_provider=None,
    )
    metadata = {
        "raw_memory_id": source.id,
        "memory_scope": scope,
        "principal_id": "user_a",
    }
    if key:
        metadata["scope_key"] = key
    if scope == "project":
        metadata["project_id"] = key
    row = task("source-backed", description="Retained dependency description", metadata=metadata)
    return source, row


async def delete_source(runtime, source):
    receipt = await apply_memory_correction(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id=source.id,
        action="delete",
        accessible_projects={"project_a"},
        writable_projects={"project_a"},
        accessible_teams={"team_a"},
        accessible_delegations={"space_a"},
    )
    assert receipt.applied and receipt.propagation_complete, receipt
    return receipt


async def read_dependencies(runtime, root="root", **kwargs):
    return await exploration.explore(
        mode="dependencies",
        entity_id=root,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        accessible_projects={"project_a"},
        accessible_teams={"team_a"},
        accessible_delegations={"space_a"},
        **kwargs,
    )


@pytest.mark.parametrize("scope", ["private", "project", "team", "delegated"])
@pytest.mark.parametrize("project_filter", [False, True])
async def test_dependencies_deny_deleted_source_root(runtime, content_store, scope, project_filter):
    source, row = await source_task(runtime, scope=scope)
    if project_filter:
        row = row.model_copy(update={"metadata": {**row.metadata, "project_id": "project_a"}})
    ordinary = task("ordinary", metadata={"project_id": "project_a"})
    await store_dependency_graph(runtime, [row, ordinary], [(row.id, ordinary.id)])
    kwargs = {"project": "project_a"} if project_filter else {}
    baseline = await read_dependencies(runtime, row.id, **kwargs)
    assert {entity.id for entity in baseline.entities} == {row.id, "ordinary"}
    assert next(entity for entity in baseline.entities if entity.id == row.id).description
    await delete_source(runtime, source)
    result = await read_dependencies(runtime, row.id, **kwargs)
    assert result.entities == []
    assert "circular_dependencies" not in result.filters


async def test_dependencies_do_not_discover_beyond_retired_frontier(
    runtime, content_store, monkeypatch
):
    source, frontier = await source_task(runtime)
    await store_dependency_graph(
        runtime,
        [task("root"), frontier, task("tail")],
        [("root", frontier.id), (frontier.id, "tail"), ("tail", "root")],
    )
    baseline = await read_dependencies(runtime)
    assert {entity.id for entity in baseline.entities} == {"root", frontier.id, "tail"}
    assert baseline.filters["circular_dependencies"] == [{"from": "root", "to": "root"}]
    await delete_source(runtime, source)
    original = runtime.relationship_manager.get_related_entities
    discovered = []

    async def track(**kwargs):
        discovered.append(kwargs["entity_id"])
        return await original(**kwargs)

    monkeypatch.setattr(runtime.relationship_manager, "get_related_entities", track)
    result = await read_dependencies(runtime)
    assert [entity.id for entity in result.entities] == ["root"]
    assert discovered == ["root"]
    assert "circular_dependencies" not in result.filters


async def test_dependencies_rebuild_reachability_after_late_source_delete(
    runtime, content_store, monkeypatch
):
    source, frontier = await source_task(runtime)
    await store_dependency_graph(
        runtime,
        [task("root"), frontier, task("tail"), task("independent")],
        [
            ("root", frontier.id),
            (frontier.id, "tail"),
            ("tail", "root"),
            ("root", "independent"),
        ],
    )
    original = runtime.relationship_manager.get_related_entities
    corrected = False

    async def delete_at_tail(**kwargs):
        nonlocal corrected
        result = await original(**kwargs)
        if kwargs["entity_id"] == "tail" and not corrected:
            corrected = True
            await delete_source(runtime, source)
        return result

    monkeypatch.setattr(runtime.relationship_manager, "get_related_entities", delete_at_tail)
    result = await read_dependencies(runtime)
    assert corrected
    assert {entity.id for entity in result.entities} == {"root", "independent"}
    assert "circular_dependencies" not in result.filters


@pytest.mark.parametrize("mode", ["related", "dependencies"])
async def test_graph_reader_revalidates_root_after_node_collection(
    runtime, content_store, monkeypatch, mode
):
    source, root = await source_task(runtime)
    kind = RelationshipType.RELATED_TO if mode == "related" else RelationshipType.DEPENDS_ON
    await store_dependency_graph(
        runtime, [root, task("ordinary")], [(root.id, "ordinary")], kind=kind
    )
    baseline = await exploration.explore(
        mode=mode,
        entity_id=root.id,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )
    assert "ordinary" in {entity.id for entity in baseline.entities}
    original = graph_view_availability.available_graph_entities
    corrected = False

    async def delete_after_collection(org, ids, **kwargs):
        nonlocal corrected
        result = await original(org, ids, **kwargs)
        if {root.id, "ordinary"} <= set(result) and not corrected:
            corrected = True
            await delete_source(runtime, source)
        return result

    monkeypatch.setattr(
        graph_view_availability, "available_graph_entities", delete_after_collection
    )
    result = await exploration.explore(
        mode=mode,
        entity_id=root.id,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )
    assert corrected
    assert result.entities == []


async def test_dependencies_keep_order_depth_cycles_and_limit(runtime, content_store):
    await store_dependency_graph(
        runtime,
        [task("root"), task("middle"), task("leaf")],
        [("root", "middle"), ("middle", "leaf"), ("leaf", "middle")],
    )
    result = await read_dependencies(runtime)
    assert [entity.id for entity in result.entities] == ["root", "middle", "leaf"]
    assert [entity.metadata["depth"] for entity in result.entities] == [0, 1, 2]
    assert [entity.metadata["is_root"] for entity in result.entities] == [True, False, False]
    assert result.filters["circular_dependencies"] == [{"from": "root", "to": "middle"}]
    assert result.filters["warning"] == "Circular dependencies detected"
    limited = await read_dependencies(runtime, limit=2)
    assert [entity.id for entity in limited.entities] == ["root", "middle"]


@pytest.mark.parametrize("mutation", ["delete", "type", "target", "retire"])
async def test_dependencies_drop_changed_followed_edge(
    runtime, content_store, monkeypatch, mutation
):
    edges = await store_dependency_graph(
        runtime,
        [task("root"), task("middle"), task("leaf"), task("other")],
        [("root", "middle"), ("middle", "leaf")],
    )
    original = runtime.relationship_manager.get_related_entities
    changed = False

    async def remove_ancestor_at_leaf(**kwargs):
        nonlocal changed
        result = await original(**kwargs)
        if kwargs["entity_id"] == "leaf" and not changed:
            changed = True
            if mutation == "delete":
                await runtime.relationship_manager.delete(edges[0].id)
            else:
                change = {
                    "type": {"relationship_type": RelationshipType.RELATED_TO},
                    "target": {"target_id": "other"},
                    "retire": {"metadata": {"excluded_from_recall": True}},
                }[mutation]
                await runtime.relationship_manager.create_direct_bulk(
                    [edges[0].model_copy(update=change)]
                )
        return result

    monkeypatch.setattr(
        runtime.relationship_manager, "get_related_entities", remove_ancestor_at_leaf
    )
    result = await read_dependencies(runtime)
    assert changed
    assert [entity.id for entity in result.entities] == ["root"]


async def test_dependencies_recheck_a_frontier_after_sibling_discovery(
    runtime, content_store, monkeypatch
):
    source, frontier = await source_task(runtime)
    await store_dependency_graph(
        runtime,
        [task("root"), task("first"), frontier, task("tail")],
        [("root", frontier.id), ("root", "first"), (frontier.id, "tail")],
    )
    original = runtime.relationship_manager.get_related_entities
    initial = await original(
        entity_id="root", relationship_types=[RelationshipType.DEPENDS_ON], max_depth=1, limit=100
    )
    assert [entity.id for entity, edge in initial if edge.source_id == "root"] == [
        "first",
        frontier.id,
    ]
    discovered = []
    corrected = False

    async def delete_during_sibling(**kwargs):
        nonlocal corrected
        discovered.append(kwargs["entity_id"])
        result = await original(**kwargs)
        if kwargs["entity_id"] == "first" and not corrected:
            corrected = True
            await delete_source(runtime, source)
        return result

    monkeypatch.setattr(runtime.relationship_manager, "get_related_entities", delete_during_sibling)
    result = await read_dependencies(runtime)
    assert corrected
    assert discovered == ["root", "first"]
    assert {entity.id for entity in result.entities} == {"root", "first"}


async def test_related_rechecks_source_backed_neighbor_after_node_collection(
    runtime, content_store, monkeypatch
):
    source, neighbor = await source_task(runtime)
    await store_dependency_graph(
        runtime,
        [task("root"), neighbor],
        [("root", neighbor.id)],
        kind=RelationshipType.RELATED_TO,
    )
    baseline = await exploration.explore(
        mode="related",
        entity_id="root",
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )
    assert {row.id for row in baseline.entities} == {neighbor.id}
    assert baseline.entities[0].name == neighbor.name
    original = graph_view_availability.available_graph_entities
    corrected = False

    async def delete_after_collection(org, ids, **kwargs):
        nonlocal corrected
        result = await original(org, ids, **kwargs)
        if {"root", neighbor.id} <= set(result) and not corrected:
            corrected = True
            await delete_source(runtime, source)
        return result

    monkeypatch.setattr(
        graph_view_availability, "available_graph_entities", delete_after_collection
    )
    result = await exploration.explore(
        mode="related",
        entity_id="root",
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )
    assert corrected
    assert result.entities == []


@pytest.mark.parametrize("mode", ["dependencies", "related"])
@pytest.mark.parametrize("change", ["delete", "type", "retire"])
async def test_graph_reader_drops_edge_changed_during_final_node_loading(monkeypatch, mode, change):
    async with dependency_graph() as runtime:
        kind = (
            RelationshipType.DEPENDS_ON if mode == "dependencies" else RelationshipType.RELATED_TO
        )
        edges = await store_dependency_graph(
            runtime, [task("root"), task("leaf")], [("root", "leaf")], kind=kind
        )
        monkeypatch.setattr(exploration, "get_graph_runtime", AsyncMock(return_value=runtime))
        kwargs = dict(mode=mode, entity_id="root", organization_id=runtime.client.group_id)
        baseline = await exploration.explore(**kwargs)
        assert "leaf" in {row.id for row in baseline.entities}
        available = graph_view_availability.available_graph_entities
        changed = False

        async def change_during_nodes(org, ids, **options):
            nonlocal changed
            nodes = await available(org, ids, **options)
            if set(nodes) == {"root", "leaf"} and not changed:
                changed = True
                if change == "delete":
                    assert await runtime.relationship_manager.delete(edges[0].id)
                else:
                    replacement = edges[0].model_copy(
                        update={"relationship_type": RelationshipType.BLOCKS}
                        if change == "type"
                        else {"metadata": {"expired_at": "2026-01-01T00:00:00+00:00"}}
                    )
                    await runtime.relationship_manager.create_direct_bulk([replacement])
            return nodes

        monkeypatch.setattr(
            graph_view_availability, "available_graph_entities", change_during_nodes
        )
        result = await exploration.explore(**kwargs)
        assert changed
        assert {row.id for row in await runtime.entity_manager.get_many(["root", "leaf"])} == {
            "root",
            "leaf",
        }
        assert [row.id for row in result.entities] == (["root"] if mode == "dependencies" else [])
