"""Native graph runtime helpers for higher-level service layers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sibyl_core.embeddings.providers import (
    EmbeddingProvider,
    configured_embedding_provider,
)
from sibyl_core.models.entities import EntityType
from sibyl_core.services.graph_client import (
    SurrealGraphClient,
    get_surreal_graph_client,
    prepare_graph_schema,
    validate_native_embedding_dimensions,
)
from sibyl_core.services.graph_common import normalize_graph_records
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.utils.query import upper_query_tokens


class EntityRecordLike(Protocol):
    entity_type: EntityType


class EntityManagerLike(Protocol):
    async def list_all(
        self,
        *,
        limit: int,
        offset: int,
        include_archived: bool,
    ) -> Sequence[EntityRecordLike]: ...


@dataclass(frozen=True)
class GraphRuntime:
    """Bound graph collaborators for a single organization."""

    client: SurrealGraphClient
    entity_manager: EntityManager
    relationship_manager: RelationshipManager


def _assert_surreal_query_dialect(query: str) -> None:
    if not upper_query_tokens(query).isdisjoint({"CALL", "MATCH", "UNWIND"}):
        raise ValueError("Surreal runtime graph queries must use SurrealQL")


async def get_surreal_graph_runtime(
    group_id: str,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    ensure_schema: bool = True,
) -> GraphRuntime:
    """Bind native graph collaborators for one organization."""

    client = await get_surreal_graph_client(group_id)
    if ensure_schema:
        await prepare_graph_schema(client)
    validate_native_embedding_dimensions(embedding_provider)
    return GraphRuntime(
        client=client,
        entity_manager=EntityManager(
            client,
            group_id=group_id,
            embedding_provider=embedding_provider,
        ),
        relationship_manager=RelationshipManager(
            client,
            group_id=group_id,
            embedding_provider=embedding_provider,
        ),
    )


async def get_graph_client(group_id: str = "default") -> SurrealGraphClient:
    """Return the native graph client for the requested organization."""

    client = await get_surreal_graph_client(str(group_id))
    await client.connect()
    return client


async def get_graph_runtime(group_id: str) -> GraphRuntime:
    """Bind the configured native graph runtime for one organization."""

    embedding_provider = configured_embedding_provider()
    if embedding_provider is None:
        runtime = await get_surreal_graph_runtime(str(group_id))
    else:
        runtime = await get_surreal_graph_runtime(
            str(group_id), embedding_provider=embedding_provider
        )
    return GraphRuntime(
        client=runtime.client,
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
    )


async def count_entities_by_type(
    entity_manager: EntityManagerLike,
    *,
    include_archived: bool = False,
    page_size: int = 1000,
) -> dict[str, int]:
    """Count entities by type, using native aggregation when available."""

    counter = getattr(entity_manager, "count_by_type", None)
    if callable(counter):
        return await counter(include_archived=include_archived)

    counts = {entity_type.value: 0 for entity_type in EntityType}
    offset = 0
    while True:
        entities = await entity_manager.list_all(
            limit=page_size,
            offset=offset,
            include_archived=include_archived,
        )
        if not entities:
            break
        for entity in entities:
            counts[entity.entity_type.value] = counts.get(entity.entity_type.value, 0) + 1
        offset += len(entities)
    return counts


async def execute_graph_query(
    group_id: str,
    query: str,
    **params: object,
) -> list[dict[str, object]]:
    """Execute a raw org-scoped graph query and normalize the result."""

    runtime = await get_graph_runtime(str(group_id))
    _assert_surreal_query_dialect(query)
    result = await runtime.client.execute_query(query, group_id=str(group_id), **params)
    return normalize_graph_records(result)


__all__ = [
    "GraphRuntime",
    "count_entities_by_type",
    "execute_graph_query",
    "get_graph_client",
    "get_graph_runtime",
    "get_surreal_graph_runtime",
]
