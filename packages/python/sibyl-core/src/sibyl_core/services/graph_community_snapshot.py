"""Cached graph snapshots and reader-scoped visibility."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    memory_row_project_id,
    private_scope_granted_for,
)
from sibyl_core.models.entities import Entity
from sibyl_core.services.graph_community_managers import (
    _list_all_entities,
    _list_all_relationships,
)
from sibyl_core.services.graph_community_models import GraphSnapshot

log = structlog.get_logger()

type _ReaderCacheKey = tuple[str, tuple[str, ...], tuple[str, ...] | None]

GRAPH_SNAPSHOT_CACHE: dict[tuple[str, int | None, int | None], tuple[datetime, GraphSnapshot]] = {}
GRAPH_SNAPSHOT_CACHE_TTL = timedelta(minutes=5)
GRAPH_SNAPSHOT_LOADS: dict[tuple[str, int | None, int | None], asyncio.Task[GraphSnapshot]] = {}
_GRAPH_SNAPSHOT_WAITERS: dict[asyncio.Task[GraphSnapshot], int] = {}


async def _get_graph_snapshot(
    client: Any,
    organization_id: str,
    *,
    max_entities: int | None = None,
    max_relationships: int | None = None,
) -> GraphSnapshot:
    cache_key = (organization_id, max_entities, max_relationships)
    cached = GRAPH_SNAPSHOT_CACHE.get(cache_key)
    if cached is not None:
        cached_at, snapshot = cached
        if datetime.now(UTC) - cached_at < GRAPH_SNAPSHOT_CACHE_TTL:
            log.debug(
                "graph_snapshot_cache_hit",
                org_id=organization_id,
                max_entities=max_entities,
                max_relationships=max_relationships,
            )
            return snapshot

    task = GRAPH_SNAPSHOT_LOADS.get(cache_key)
    if task is not None:
        log.debug(
            "graph_snapshot_load_joined",
            org_id=organization_id,
            max_entities=max_entities,
            max_relationships=max_relationships,
        )
    else:
        task = asyncio.create_task(
            _load_graph_snapshot(
                client,
                organization_id,
                max_entities=max_entities,
                max_relationships=max_relationships,
            )
        )
        GRAPH_SNAPSHOT_LOADS[cache_key] = task

    _GRAPH_SNAPSHOT_WAITERS[task] = _GRAPH_SNAPSHOT_WAITERS.get(task, 0) + 1
    try:
        # Every caller is a peer waiter. Shielding prevents one cancelled
        # request from deciding the lifetime of work another request needs.
        return await asyncio.shield(task)
    finally:
        remaining = _GRAPH_SNAPSHOT_WAITERS[task] - 1
        if remaining > 0:
            _GRAPH_SNAPSHOT_WAITERS[task] = remaining
        else:
            _GRAPH_SNAPSHOT_WAITERS.pop(task, None)
            if GRAPH_SNAPSHOT_LOADS.get(cache_key) is task:
                GRAPH_SNAPSHOT_LOADS.pop(cache_key, None)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    log.debug(
                        "graph_snapshot_cancelled_loader_failed",
                        org_id=organization_id,
                        error=str(error),
                    )


async def _load_graph_snapshot(
    client: Any,
    organization_id: str,
    *,
    max_entities: int | None,
    max_relationships: int | None,
) -> GraphSnapshot:
    entities, relationships = await asyncio.gather(
        _list_all_entities(
            client,
            organization_id,
            batch_size=max_entities or 1000,
            max_items=max_entities,
        ),
        _list_all_relationships(
            client,
            organization_id,
            batch_size=max_relationships or 1000,
            max_items=max_relationships,
        ),
    )
    entity_by_id = _entity_index(entities)
    snapshot = GraphSnapshot(
        entities=entities,
        relationships=relationships,
        entity_by_id=entity_by_id,
    )
    GRAPH_SNAPSHOT_CACHE[(organization_id, max_entities, max_relationships)] = (
        datetime.now(UTC),
        snapshot,
    )
    log.info(
        "graph_snapshot_cache_updated",
        org_id=organization_id,
        entity_count=len(entities),
        relationship_count=len(relationships),
        max_entities=max_entities,
        max_relationships=max_relationships,
    )
    return snapshot


def _entity_index(entities: list[Entity]) -> dict[str, Entity]:
    return {entity.id: entity for entity in entities if entity.id}


def _reader_cache_key(
    principal_id: str | None,
    accessible_projects: set[str] | None,
    allowed_memory_scope_keys: set[str] | None = None,
) -> tuple[str, tuple[str, ...], tuple[str, ...] | None]:
    """Identity component for every cache holding reader-visible graph rows.

    Detection input, cluster summaries and rendered levels of detail are all
    derived from a filtered snapshot, so an org-only key would serve one
    reader's allowed set to the next reader who asks.
    """
    return (
        str(principal_id or ""),
        tuple(sorted(str(project_id) for project_id in accessible_projects or ())),
        None
        if allowed_memory_scope_keys is None
        else tuple(sorted(str(key) for key in allowed_memory_scope_keys)),
    )


def _reader_visible_snapshot(
    snapshot: GraphSnapshot,
    *,
    principal_id: str | None,
    accessible_projects: set[str] | None,
    allowed_memory_scope_keys: set[str] | None = None,
) -> GraphSnapshot:
    """Reduce a snapshot to the rows this reader is authorized to see.

    The scope predicate is expressible in SurrealQL against the flexible
    attributes object, but pushing it down would restate a policy whose
    branches (owner as principal_id or scope_key, unrecognized scopes denied,
    team and delegated scopes closed pending their membership threads) already
    live in memory_metadata_read_allowed. Two implementations in two languages
    is the drift this filter exists to prevent, so the snapshot loads whole and
    is narrowed here, once, through the shared rule.
    """
    entities = [
        entity
        for entity in snapshot.entities
        if memory_metadata_read_allowed(
            getattr(entity, "metadata", None),
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            private_scope_granted=private_scope_granted_for(
                allowed_memory_scope_keys, principal_id=principal_id
            ),
            row_project_id=memory_row_project_id(
                getattr(entity, "metadata", None),
                entity_type=getattr(getattr(entity, "entity_type", None), "value", None),
                entity_id=getattr(entity, "id", None),
            ),
        )
    ]
    entity_by_id = _entity_index(entities)
    relationships = [
        relationship
        for relationship in snapshot.relationships
        if relationship.source_id in entity_by_id and relationship.target_id in entity_by_id
    ]
    return GraphSnapshot(
        entities=entities,
        relationships=relationships,
        entity_by_id=entity_by_id,
    )


async def _get_visible_graph_snapshot(
    client: Any,
    organization_id: str,
    *,
    principal_id: str | None,
    accessible_projects: set[str] | None,
    allowed_memory_scope_keys: set[str] | None = None,
    max_entities: int | None = None,
    max_relationships: int | None = None,
) -> GraphSnapshot:
    snapshot = await _get_graph_snapshot(
        client,
        organization_id,
        max_entities=max_entities,
        max_relationships=max_relationships,
    )
    return _reader_visible_snapshot(
        snapshot,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )


def _count_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


async def _native_rows(
    client: Any,
    organization_id: str,
    query: str,
    **params: object,
) -> list[dict[str, object]] | None:
    execute_query = getattr(client, "execute_query", None)
    if not callable(execute_query):
        return None

    try:
        result = execute_query(query, group_id=organization_id, **params)
        if not inspect.isawaitable(result):
            return None
        from sibyl_core.services.graph_common import normalize_graph_records

        return normalize_graph_records(await result)
    except Exception as exc:
        log.warning("native_graph_query_failed", org_id=organization_id, error=str(exc))
        return None


async def _graph_totals(
    client: Any,
    organization_id: str,
) -> tuple[int, int] | None:
    from sibyl_core.services.graph_client import SurrealGraphClient
    from sibyl_core.services.graph_common import normalize_graph_records

    if not isinstance(client, SurrealGraphClient):
        return None

    try:
        rows = normalize_graph_records(
            await client.execute_query(
                """
                RETURN {
                    total_nodes: count(
                        SELECT VALUE uuid
                        FROM entity
                        WHERE group_id = $group_id
                    ),
                    total_edges: count(
                        SELECT VALUE uuid
                        FROM relates_to
                        WHERE group_id = $group_id
                    )
                };
                """,
                group_id=organization_id,
            )
        )
    except Exception as exc:
        log.warning("graph_totals_failed", org_id=organization_id, error=str(exc))
        return None

    if not rows:
        return None
    row = rows[0]
    return _count_int(row.get("total_nodes")), _count_int(row.get("total_edges"))
