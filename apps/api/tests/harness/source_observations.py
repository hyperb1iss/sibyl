"""Isolated graph sources for route tests exercising trusted materialization."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.models.entities import Entity
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime


@asynccontextmanager
async def observed_graph_sources(
    organization_id: str, entities: Sequence[Entity]
) -> AsyncIterator[GraphRuntime]:
    client = SurrealGraphClient(group_id=organization_id, url="memory://")
    try:
        await bootstrap_schema(client)
        runtime = GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=organization_id),
            relationship_manager=RelationshipManager(client, group_id=organization_id),
        )
        for entity in entities:
            await runtime.entity_manager.create_direct(entity)
        with patch(
            "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
            AsyncMock(return_value=runtime),
        ):
            yield runtime
    finally:
        await client.close()
