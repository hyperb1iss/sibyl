"""Supersession enforcement exercised against a real embedded SurrealDB graph.

The unit suite in `test_supersession_enforcement.py` stubs the Surreal query,
which proves the Python branches but not the SQL, the edge direction as
actually persisted, or the admission lanes that rebuild item metadata from an
entity rather than from a search result. These tests write real rows and real
`relates_to` edges through the production managers and then read them back
through `context_search` and `compile_context`.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

import sibyl_core.retrieval.search as search_module
import sibyl_core.tools.context as context_module
from sibyl_core.auth.memory_policy import stamp_memory_scope_metadata
from sibyl_core.models.context import ContextFacet
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.retrieval.search import build_context_retrieval_plan
from sibyl_core.services.graph import (
    EntityManager,
    RelationshipManager,
    SurrealGraphClient,
    prepare_graph_schema,
)

PROJECT_ID = "proj-supersession"
PRINCIPAL = "user-supersession"


class _Runtime:
    def __init__(self, client: Any, entities: Any, relationships: Any, group_id: str) -> None:
        self.client = client
        self.entity_manager = entities
        self.relationship_manager = relationships
        self.group_id = group_id


@pytest_asyncio.fixture
async def graph(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Runtime]:
    # Each test gets its own namespace. The embedded store is process-wide, so
    # a shared group id leaks rows and edges between tests and turns a
    # supersession assertion into a function of test ordering.
    group_id = f"supersession-live-{uuid_module.uuid4().hex[:12]}"
    client = SurrealGraphClient(group_id=group_id, url="memory://")
    await client.connect()
    await prepare_graph_schema(client)
    runtime = _Runtime(
        client,
        EntityManager(client, group_id=group_id),
        RelationshipManager(client, group_id=group_id),
        group_id,
    )

    async def runtime_factory(requested_group_id: str, **_kwargs: object) -> _Runtime:
        assert str(requested_group_id) == group_id
        return runtime

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", runtime_factory)
    monkeypatch.setattr(context_module, "get_surreal_graph_runtime", runtime_factory)
    monkeypatch.setattr(context_module, "get_graph_runtime", runtime_factory, raising=False)
    yield runtime
    await client.close()


def _entity(runtime: _Runtime, uuid: str, name: str, content: str, **metadata: Any) -> Entity:
    return Entity(
        id=uuid,
        name=name,
        entity_type=EntityType.DECISION,
        description=content,
        content=content,
        organization_id=runtime.group_id,
        metadata={
            "memory_scope": "project",
            "scope_key": PROJECT_ID,
            "project_id": PROJECT_ID,
            **metadata,
        },
        project_id=PROJECT_ID,
    )


async def _supersede(runtime: _Runtime, *, survivor: str, retired: str) -> None:
    """Write the edge in the direction the production write path uses."""

    await runtime.relationship_manager.create(
        Relationship(
            id=f"rel_{survivor}_supersedes_{retired}",
            source_id=survivor,
            target_id=retired,
            relationship_type=RelationshipType.SUPERSEDES,
            organization_id=runtime.group_id,
            metadata={"native_write_path": "memory_correction"},
        )
    )


def _plan(runtime: _Runtime, query: str) -> Any:
    return build_context_retrieval_plan(
        query=query,
        organization_id=runtime.group_id,
        facets=[ContextFacet.DECISIONS],
        facet_types={ContextFacet.DECISIONS: ["decision"]},
        principal_id=PRINCIPAL,
        project=PROJECT_ID,
        accessible_projects={PROJECT_ID},
        limit=12,
    )


async def _search(runtime: _Runtime, query: str) -> Any:
    return await search_module.context_search(
        plan=_plan(runtime, query),
        types=["decision"],
        facet=ContextFacet.DECISIONS,
        limit=10,
        include_content=True,
        raw_memory_recall_fn=lambda **_kwargs: [],
    )


@pytest.mark.asyncio
async def test_a_real_supersedes_edge_retires_its_target_and_serves_the_successor(
    graph: _Runtime,
) -> None:
    """End to end through real SQL: the retired row leaves, the survivor stays."""

    await graph.entity_manager.create_direct(
        _entity(graph, "live-old", "Deploy to Fly", "we deploy to fly for hosting")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "live-new", "Deploy to Hetzner", "we deploy to hetzner for hosting")
    )
    await _supersede(graph, survivor="live-new", retired="live-old")

    response = await _search(graph, "deploy hosting")

    served = [result.id for result in response.results]
    assert "live-new" in served
    assert "live-old" not in served
    assert response.filters["supersession_gate"]["superseded_uuids"] == ["live-old"]


@pytest.mark.asyncio
async def test_a_real_supersession_cycle_does_not_black_out_both_rows(
    graph: _Runtime,
) -> None:
    """Mutual supersession leaves the newest statement standing, not nothing."""

    await graph.entity_manager.create_direct(
        _entity(graph, "cycle-a", "Cycle A", "cycle hosting decision alpha")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "cycle-b", "Cycle B", "cycle hosting decision beta")
    )
    await _supersede(graph, survivor="cycle-a", retired="cycle-b")
    await _supersede(graph, survivor="cycle-b", retired="cycle-a")

    response = await _search(graph, "cycle hosting decision")

    served = {result.id for result in response.results}
    assert served, "a supersession cycle must not black out every row"
    assert len(served & {"cycle-a", "cycle-b"}) == 1


@pytest.mark.asyncio
async def test_a_real_self_supersession_retires_nothing(graph: _Runtime) -> None:
    """A row that supersedes itself must stay servable."""

    await graph.entity_manager.create_direct(
        _entity(graph, "selfie", "Self Superseding", "self superseding hosting note")
    )
    await _supersede(graph, survivor="selfie", retired="selfie")

    response = await _search(graph, "self superseding hosting")

    assert "selfie" in {result.id for result in response.results}


@pytest.mark.asyncio
async def test_the_outgoing_walk_does_not_expand_into_a_real_retired_row(
    graph: _Runtime,
) -> None:
    """Traversal exclusion, proven against the real relates_to table."""

    await graph.entity_manager.create_direct(
        _entity(graph, "walk-new", "Walk Survivor", "walk survivor hosting body")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "walk-old", "Walk Retired", "walk retired hosting body")
    )
    await _supersede(graph, survivor="walk-new", retired="walk-old")

    hops = await search_module._node_bfs_records(
        client=graph.client,
        origin_uuids=["walk-new"],
        search_filter=search_module.SearchFilter(node_types=("decision",)),
        group_id=graph.group_id,
        max_depth=1,
        limit=10,
    )
    assert [row.get("uuid") for row in hops] == []

    # Naming the predicate is the documented carve-out for lineage walks.
    lineage = await search_module._node_bfs_records(
        client=graph.client,
        origin_uuids=["walk-new"],
        search_filter=search_module.SearchFilter(node_types=("decision",)),
        group_id=graph.group_id,
        max_depth=1,
        limit=10,
        relationship_names=["SUPERSEDES"],
    )
    assert [row.get("uuid") for row in lineage] == ["walk-old"]


@pytest.mark.asyncio
async def test_deleting_the_edge_restores_the_row_to_recall(graph: _Runtime) -> None:
    """The supersede/restore round trip, which is what makes correction reversible."""

    await graph.entity_manager.create_direct(
        _entity(graph, "round-old", "Round Retired", "round trip hosting body")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "round-new", "Round Survivor", "round trip hosting successor")
    )
    await _supersede(graph, survivor="round-new", retired="round-old")

    assert "round-old" not in {r.id for r in (await _search(graph, "round trip hosting")).results}

    await graph.client.execute_query(
        """
        DELETE FROM relates_to
        WHERE group_id = $group_id
          AND name = 'SUPERSEDES'
          AND target_id IN $target_ids
          AND attributes.native_write_path = 'memory_correction';
        """,
        group_id=graph.group_id,
        target_ids=["round-old"],
    )

    assert "round-old" in {r.id for r in (await _search(graph, "round trip hosting")).results}


@pytest.mark.asyncio
async def test_the_active_work_lane_cannot_smuggle_a_retired_task_into_a_pack(
    graph: _Runtime,
) -> None:
    """This lane rebuilds item metadata from the entity, so it needs its own carry."""

    live = Entity(
        id="task-live",
        name="Live Task",
        entity_type=EntityType.TASK,
        description="live task body",
        content="live task body",
        organization_id=graph.group_id,
        status="doing",
        project_id=PROJECT_ID,
        metadata={"memory_scope": "project", "scope_key": PROJECT_ID, "project_id": PROJECT_ID},
    )
    retired = Entity(
        id="task-retired",
        name="Retired Task",
        entity_type=EntityType.TASK,
        description="retired task body",
        content="retired task body",
        organization_id=graph.group_id,
        status="doing",
        project_id=PROJECT_ID,
        metadata={
            "memory_scope": "project",
            "scope_key": PROJECT_ID,
            "project_id": PROJECT_ID,
            "lifecycle_state": "contested",
            "excluded_from_recall": True,
        },
    )
    await graph.entity_manager.create_direct(live)
    await graph.entity_manager.create_direct(retired)

    # Load the rows back out of the graph so the item builder sees exactly what
    # production hands it, then run the real builder and the real admission
    # filter. The lookup query itself is not what this lane got wrong: the
    # builder was rebuilding item metadata from scratch and dropping the
    # lifecycle fields, so admission had nothing to read.
    loaded = [
        await graph.entity_manager.get("task-live"),
        await graph.entity_manager.get("task-retired"),
    ]
    items = [context_module._item_from_active_entity(entity) for entity in loaded]
    carried = {item.id: item.metadata for item in items}
    assert carried["task-retired"].get("excluded_from_recall") is True
    assert carried["task-retired"].get("lifecycle_state") == "contested"
    assert "excluded_from_recall" not in carried["task-live"]

    sections = context_module._drop_retired_items(
        [
            context_module.ContextSection(
                facet=ContextFacet.ACTIVE_WORK,
                title="Active work",
                items=items,
            )
        ]
    )
    served = [item.id for section in sections for item in section.items]
    assert served == ["task-live"]


@pytest.mark.asyncio
async def test_the_related_item_lane_does_not_attach_a_retired_neighbour(
    graph: _Runtime,
) -> None:
    """Related items ride inside an admitted item, so admission never sees them."""

    await graph.entity_manager.create_direct(
        _entity(graph, "seed-row", "Seed Row", "seed row hosting body")
    )
    await graph.entity_manager.create_direct(
        _entity(graph, "neighbour-live", "Live Neighbour", "live neighbour body")
    )
    await graph.entity_manager.create_direct(
        _entity(
            graph,
            "neighbour-retired",
            "Retired Neighbour",
            "retired neighbour body",
            lifecycle_state="contested",
            excluded_from_recall=True,
        )
    )
    for target in ("neighbour-live", "neighbour-retired"):
        await graph.relationship_manager.create(
            Relationship(
                id=f"rel_seed_{target}",
                source_id="seed-row",
                target_id=target,
                relationship_type=RelationshipType.RELATED_TO,
                organization_id=graph.group_id,
                metadata={},
            )
        )

    related = await context_module._default_related_items(
        entity_id="seed-row",
        organization_id=graph.group_id,
        accessible_projects={PROJECT_ID},
        principal_id=PRINCIPAL,
        limit=5,
    )

    assert [item.id for item in related] == ["neighbour-live"]


def test_a_write_payload_cannot_supply_capture_provenance() -> None:
    """Provenance is what the correction write-through resolves its targets by.

    Leaving it caller-settable is what let a planted `raw_memory_id` nominate
    somebody else's row to be retired by a correction on an unrelated capture.
    """

    stamped = stamp_memory_scope_metadata(
        {
            "raw_memory_id": "victim-capture",
            "raw_source_id": "victim-source",
            "principal_id": "somebody-else",
            "note": "kept",
        },
        memory_scope="project",
        scope_key=PROJECT_ID,
        principal_id=PRINCIPAL,
    )

    assert "raw_memory_id" not in stamped
    assert "raw_source_id" not in stamped
    assert stamped["principal_id"] == PRINCIPAL
    assert stamped["note"] == "kept"
