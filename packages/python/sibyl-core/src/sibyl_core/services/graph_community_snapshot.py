"""Cached graph snapshots and reader-scoped visibility."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sibyl_core.models.entities import Entity
from sibyl_core.services.graph_community_managers import (
    _list_all_entities,
    _list_all_relationships,
)
from sibyl_core.services.graph_community_models import GraphSnapshot
from sibyl_core.services.graph_visibility import graph_row_read_allowed

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

    def allowed(row):
        return graph_row_read_allowed(
            row,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
        )

    entities = [entity for entity in snapshot.entities if allowed(entity)]
    entity_by_id = _entity_index(entities)
    relationships = [
        relationship
        for relationship in snapshot.relationships
        if relationship.source_id in entity_by_id
        and relationship.target_id in entity_by_id
        and allowed(relationship)
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
    snapshot = await _current_graph_snapshot(client, organization_id, snapshot)
    return _reader_visible_snapshot(
        snapshot,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )


async def _current_graph_entities(
    client: Any, organization_id: str, ids: list[str]
) -> dict[str, Entity]:
    """Keep current-row and ancestry reads on the supplied graph owner."""
    from sibyl_core.services.graph_community_managers import (
        _entity_manager_for_client,
        _relationship_manager_for_client,
    )
    from sibyl_core.services.graph_read_availability import available_graph_entities
    from sibyl_core.services.graph_runtime import GraphRuntime

    runtime = GraphRuntime(
        client=client,
        entity_manager=_entity_manager_for_client(client, organization_id),
        relationship_manager=_relationship_manager_for_client(client, organization_id),
    )
    return await available_graph_entities(organization_id, ids, runtime=runtime)


async def _current_graph_snapshot(
    client: Any, organization_id: str, snapshot: GraphSnapshot
) -> GraphSnapshot:
    """Refresh cached identities before their content enters a reader cache.

    Enumeration stays cached. Current rows and protected ancestry determine
    what can be rendered; a replacement using the same ID cannot revive an
    older cached label or relationship fact.
    """
    from sibyl_core.services.graph_common import normalize_graph_records
    from sibyl_core.services.graph_records import relationship_from_surreal_row

    entities = await _current_graph_entities(client, organization_id, list(snapshot.entity_by_id))
    edge_ids = [relationship.id for relationship in snapshot.relationships]
    relationships = []
    if edge_ids:
        rows = normalize_graph_records(
            await client.execute_query(
                "SELECT * FROM relates_to WHERE group_id=$group_id AND uuid IN $ids;",
                group_id=organization_id,
                ids=edge_ids,
            )
        )
        relationships = [relationship_from_surreal_row(row) for row in rows]
    return GraphSnapshot(
        entities=list(entities.values()),
        relationships=[
            relationship
            for relationship in relationships
            if relationship.source_id in entities and relationship.target_id in entities
        ],
        entity_by_id=entities,
    )


def _snapshot_fingerprint(snapshot: GraphSnapshot) -> str:
    """Bind derived cache values to their current, authorized input content."""
    payload = {
        "entities": [
            entity.model_dump(mode="json", exclude={"embedding"})
            for entity in sorted(snapshot.entities, key=lambda entity: entity.id)
        ],
        "relationships": [
            relationship.model_dump(mode="json", exclude={"embedding"})
            for relationship in sorted(
                snapshot.relationships, key=lambda relationship: relationship.id
            )
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
