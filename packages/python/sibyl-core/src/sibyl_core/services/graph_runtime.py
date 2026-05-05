"""Graph runtime helpers for higher-level service layers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import structlog

from sibyl_core.graph.client import GraphClient
from sibyl_core.models.entities import EntityType
from sibyl_core.utils.query import upper_query_tokens

if TYPE_CHECKING:
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.graph.relationships import RelationshipManager

log = structlog.get_logger()
_SURREAL_SCHEMA_PREPARED_GROUPS: set[str] = set()
_SURREAL_SCHEMA_PREPARE_LOCK = asyncio.Lock()


class QueryDriver(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


class CloneableDriver(QueryDriver, Protocol):
    def clone(self, group_id: str) -> QueryDriver: ...


class SchemaDriver(Protocol):
    async def build_indices_and_constraints(self) -> None: ...


class GraphitiClientLike(Protocol):
    driver: CloneableDriver


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
class ActiveGraphRuntime:
    """Bound graph collaborators for a single organization."""

    client: GraphClient
    entity_manager: EntityManager
    relationship_manager: RelationshipManager


def _is_surreal_driver(driver: object) -> bool:
    try:
        from sibyl_core.backends.surreal import SurrealDriver
    except ImportError:
        return False

    return isinstance(driver, SurrealDriver)


def _query_tokens(query: str) -> set[str]:
    return upper_query_tokens(query)


def _assert_surreal_query_dialect(driver: object, query: str) -> None:
    if not _is_surreal_driver(driver):
        return
    if not _query_tokens(query).isdisjoint({"CALL", "MATCH", "UNWIND"}):
        raise ValueError("Surreal runtime graph queries must use SurrealQL")


async def get_graph_client() -> GraphClient:
    """Return the shared graph client for the active store."""

    from sibyl_core.graph.client import get_graph_client

    return await get_graph_client()


async def get_graph_runtime(group_id: str) -> ActiveGraphRuntime:
    """Bind the active graph managers for a single organization."""

    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.graph.relationships import RelationshipManager

    client = await get_graph_client()
    await _prepare_surreal_graph_schema(client, group_id)
    return ActiveGraphRuntime(
        client=client,
        entity_manager=EntityManager(client, group_id=group_id),
        relationship_manager=RelationshipManager(client, group_id=group_id),
    )


async def _prepare_surreal_graph_schema(client: GraphClient, group_id: str) -> None:
    if group_id in _SURREAL_SCHEMA_PREPARED_GROUPS:
        return

    driver = client.get_org_driver(group_id)
    if not _is_surreal_driver(driver):
        return
    schema_driver = cast("SchemaDriver", driver)

    async with _SURREAL_SCHEMA_PREPARE_LOCK:
        if group_id in _SURREAL_SCHEMA_PREPARED_GROUPS:
            return
        try:
            await schema_driver.build_indices_and_constraints()
        except Exception as exc:
            log.warning(
                "surreal_graph_schema_prepare_failed",
                group_id=group_id,
                error_type=type(exc).__name__,
            )
            return
        _SURREAL_SCHEMA_PREPARED_GROUPS.add(group_id)


async def count_entities_by_type(
    entity_manager: EntityManagerLike,
    *,
    include_archived: bool = False,
    page_size: int = 1000,
) -> dict[str, int]:
    """Count entities by type without assuming backend-specific aggregations."""

    counts = {entity_type.value: 0 for entity_type in EntityType}
    driver_obj = getattr(entity_manager, "_driver", None)
    group_id = getattr(entity_manager, "_group_id", None)

    if driver_obj is not None and isinstance(group_id, str):
        driver = cast("QueryDriver", driver_obj)
        try:
            if _is_surreal_driver(driver):
                rows = GraphClient.normalize_result(
                    await driver.execute_query(
                        """
                        SELECT entity_type, count() AS cnt
                        FROM entity
                        WHERE group_id = $group_id
                        GROUP BY entity_type;
                        """,
                        group_id=group_id,
                    )
                )
            else:
                rows = GraphClient.normalize_result(
                    await driver.execute_query(
                        """
                        MATCH (n)
                        WHERE n.group_id = $group_id AND n.entity_type IS NOT NULL
                        RETURN n.entity_type AS entity_type, count(*) AS cnt
                        """,
                        group_id=group_id,
                    )
                )

            for row in rows:
                entity_type = row.get("entity_type")
                if entity_type:
                    counts[str(entity_type)] = _count_value(row.get("cnt"))
            return counts
        except Exception:
            pass

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


def _count_value(value: object) -> int:
    if isinstance(value, int | float | str):
        return int(value)
    return 0


async def execute_graph_query(
    group_id: str,
    query: str,
    **params: object,
) -> list[dict[str, object]]:
    """Execute a raw org-scoped graph query and normalize the result."""

    client = await get_graph_client()
    graphiti_client = cast("GraphitiClientLike", client.client)
    driver = graphiti_client.driver.clone(group_id)
    _assert_surreal_query_dialect(driver, query)
    result = await driver.execute_query(query, **params)
    return cast("list[dict[str, object]]", client.normalize_result(result))
