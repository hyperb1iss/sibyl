"""Connection counts seek each endpoint column instead of scanning every edge."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sibyl.persistence import graph_runtime
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType

pytestmark = pytest.mark.asyncio


class _EdgeClient:
    def __init__(self, edges: list[tuple[str, str, str]]) -> None:
        self.edges = edges
        self.statements: list[tuple[str, dict]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.statements.append((query, params))
        column = "source_id" if "source_id IN" in query else "target_id"
        wanted = set(params["entity_ids"])  # type: ignore[arg-type]
        return [
            {"uuid": uuid}
            for uuid, source, target in self.edges
            if (source if column == "source_id" else target) in wanted
        ]


async def test_connection_counts_seek_each_endpoint_column_in_batches(monkeypatch) -> None:
    ids = [f"node-{index:02d}" for index in range(40)]
    edges = [(f"edge-{index}", ids[index], ids[(index + 1) % 40]) for index in range(40)]
    client = _EdgeClient(edges)
    runtime = SimpleNamespace(
        client=client, entity_manager=SimpleNamespace(), relationship_manager=SimpleNamespace()
    )
    adapter = graph_runtime.GraphQueryAdapter(runtime, "org-counts")
    stored = {
        uuid: Relationship(
            id=uuid,
            source_id=source,
            target_id=target,
            relationship_type=RelationshipType.RELATED_TO,
        )
        for uuid, source, target in edges
    }

    async def current_edges(org, edge_ids, *, runtime):
        return {uuid: stored[uuid] for uuid in edge_ids}

    async def current_entities(org, entity_ids, *, runtime, source_visible=None, **_):
        return {
            identifier: Entity(id=identifier, name=identifier, entity_type=EntityType.TOPIC)
            for identifier in entity_ids
        }

    monkeypatch.setattr(
        "sibyl_core.services.graph_read_availability.available_graph_relationships", current_edges
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_read_availability.available_graph_entities", current_entities
    )

    counts = await adapter.get_connection_counts(ids, entity_visible=lambda row: True)

    assert counts == dict.fromkeys(ids, 2)
    assert all(" OR " not in query for query, _ in client.statements)
    assert all(len(params["entity_ids"]) <= 32 for _, params in client.statements)
    assert len(client.statements) == 4
    assert sum("source_id IN" in query for query, _ in client.statements) == 2
    assert sum("target_id IN" in query for query, _ in client.statements) == 2
