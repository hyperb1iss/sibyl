"""Cached graph snapshots and reader-scoped visibility."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Iterator, MutableMapping
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import structlog

from sibyl_core.models.entities import Entity, Relationship
from sibyl_core.services.graph_cache_invalidation import (
    graph_generation,
    register_invalidation_listener,
)
from sibyl_core.services.graph_community_managers import (
    _list_all_entities,
    _list_all_relationships,
)
from sibyl_core.services.graph_community_models import GraphSnapshot
from sibyl_core.services.graph_visibility import graph_row_read_allowed

log = structlog.get_logger()

type _ReaderCacheKey = tuple[
    str, tuple[str, ...], tuple[str, ...] | None, tuple[str, ...], tuple[str, ...]
]
type _SnapshotKey = tuple[str, int | None, int | None]
type _VisibleSnapshotKey = tuple[str, int | None, int | None, _ReaderCacheKey]


class BoundedTTLCache[K, V](MutableMapping[K, V]):
    """Insertion-ordered mapping that evicts by size and by age.

    Every graph cache holds whole-organization material keyed per reader,
    per filter or per caps, so an unbounded dict grows with every distinct
    reader for the life of the process. Reads refresh an entry's recency and
    refuse one older than the TTL; writes sweep expired entries and then
    evict the least recently used until the bound holds. Each cache keeps
    its own age check on the stamp it stores, so the TTL here is the memory
    bound, not the freshness rule.
    """

    def __init__(self, *, maxsize: int, ttl: timedelta) -> None:
        self.maxsize = max(int(maxsize), 1)
        self.ttl = ttl.total_seconds()
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def _expired(self, stamp: float) -> bool:
        return time.monotonic() - stamp >= self.ttl

    def __getitem__(self, key: K) -> V:
        stamp, value = self._entries[key]
        if self._expired(stamp):
            del self._entries[key]
            raise KeyError(key)
        self._entries.move_to_end(key)
        return value

    def __setitem__(self, key: K, value: V) -> None:
        self._entries[key] = (time.monotonic(), value)
        self._entries.move_to_end(key)
        for stale in [key for key, (stamp, _) in self._entries.items() if self._expired(stamp)]:
            del self._entries[stale]
        while len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)

    def __delitem__(self, key: K) -> None:
        del self._entries[key]

    def __iter__(self) -> Iterator[K]:
        return iter(list(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def copy(self) -> dict[K, V]:
        return {key: value for key, (_, value) in self._entries.items()}


GRAPH_SNAPSHOT_CACHE_TTL = timedelta(minutes=5)
GRAPH_SNAPSHOT_CACHE_SIZE = 16
GRAPH_SNAPSHOT_CACHE: BoundedTTLCache[_SnapshotKey, tuple[datetime, GraphSnapshot]] = (
    BoundedTTLCache(maxsize=GRAPH_SNAPSHOT_CACHE_SIZE, ttl=GRAPH_SNAPSHOT_CACHE_TTL)
)
GRAPH_SNAPSHOT_LOADS: dict[_SnapshotKey, asyncio.Task[GraphSnapshot]] = {}
_GRAPH_SNAPSHOT_WAITERS: dict[asyncio.Task[GraphSnapshot], int] = {}

# The validated, reader-visible snapshot: the enumeration above plus the
# current-row, ancestry and scope proofs, which run under the reader's source
# visibility and so are keyed by reader. An entry records the enumeration it
# was proven against and stays valid only while that same enumeration is the
# cached one: a write drops the enumeration through the generation listener,
# the enumeration TTL ages it out, and either retires the proofs with it.
GRAPH_VISIBLE_SNAPSHOT_CACHE_SIZE = 64
GRAPH_VISIBLE_SNAPSHOT_CACHE: BoundedTTLCache[
    _VisibleSnapshotKey, tuple[GraphSnapshot, GraphSnapshot]
] = BoundedTTLCache(maxsize=GRAPH_VISIBLE_SNAPSHOT_CACHE_SIZE, ttl=GRAPH_SNAPSHOT_CACHE_TTL)
GRAPH_VISIBLE_SNAPSHOT_LOADS: dict[_VisibleSnapshotKey, asyncio.Task[GraphSnapshot]] = {}
_GRAPH_VISIBLE_SNAPSHOT_WAITERS: dict[asyncio.Task[GraphSnapshot], int] = {}


def _drop_organization[K: tuple[Any, ...], V](
    cache: BoundedTTLCache[K, V], organization_id: str
) -> None:
    for key in [key for key in cache if key[0] == organization_id]:
        cache.pop(key, None)


def _drop_organization_entries(organization_id: str) -> None:
    _drop_organization(GRAPH_SNAPSHOT_CACHE, organization_id)
    _drop_organization(GRAPH_VISIBLE_SNAPSHOT_CACHE, organization_id)


register_invalidation_listener(_drop_organization_entries)


async def _shared_load[K, T](
    loads: dict[K, asyncio.Task[T]],
    waiters: dict[asyncio.Task[T], int],
    key: K,
    start: Callable[[], Coroutine[Any, Any, T]],
    *,
    event: str,
    organization_id: str,
) -> T:
    """Join the in-flight load for a key, or start it, as one of its peer waiters."""
    task = loads.get(key)
    if task is not None:
        log.debug(f"{event}_joined", org_id=organization_id)
    else:
        task = asyncio.create_task(start())
        loads[key] = task

    waiters[task] = waiters.get(task, 0) + 1
    try:
        # Every caller is a peer waiter. Shielding prevents one cancelled
        # request from deciding the lifetime of work another request needs.
        return await asyncio.shield(task)
    finally:
        remaining = waiters[task] - 1
        if remaining > 0:
            waiters[task] = remaining
        else:
            waiters.pop(task, None)
            if loads.get(key) is task:
                loads.pop(key, None)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    log.debug(
                        f"{event}_cancelled_loader_failed",
                        org_id=organization_id,
                        error=str(error),
                    )


def _fresh_snapshot(cached: tuple[datetime, GraphSnapshot] | None) -> GraphSnapshot | None:
    if cached is None:
        return None
    cached_at, snapshot = cached
    if datetime.now(UTC) - cached_at < GRAPH_SNAPSHOT_CACHE_TTL:
        return snapshot
    return None


async def _get_graph_snapshot(
    client: Any,
    organization_id: str,
    *,
    max_entities: int | None = None,
    max_relationships: int | None = None,
) -> GraphSnapshot:
    cache_key = (organization_id, max_entities, max_relationships)
    snapshot = _fresh_snapshot(GRAPH_SNAPSHOT_CACHE.get(cache_key))
    if snapshot is not None:
        log.debug(
            "graph_snapshot_cache_hit",
            org_id=organization_id,
            max_entities=max_entities,
            max_relationships=max_relationships,
        )
        return snapshot

    return await _shared_load(
        GRAPH_SNAPSHOT_LOADS,
        _GRAPH_SNAPSHOT_WAITERS,
        cache_key,
        lambda: _load_graph_snapshot(
            client,
            organization_id,
            max_entities=max_entities,
            max_relationships=max_relationships,
        ),
        event="graph_snapshot_load",
        organization_id=organization_id,
    )


async def _load_graph_snapshot(
    client: Any,
    organization_id: str,
    *,
    max_entities: int | None,
    max_relationships: int | None,
) -> GraphSnapshot:
    generation = graph_generation(organization_id)
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
    # Enumerated rows are already body-free by projection; compacting here
    # keeps that true for any loader, since every reader snapshot of this
    # organization will reference these same objects.
    entities = [_compact_entity(entity) for entity in entities]
    entity_by_id = _entity_index(entities)
    snapshot = GraphSnapshot(
        entities=entities,
        relationships=relationships,
        entity_by_id=entity_by_id,
        relationship_by_id={edge.id: edge for edge in relationships if edge.id},
    )
    if graph_generation(organization_id) != generation:
        # A write landed while the pages were read. The callers that waited
        # get this snapshot; the next one re-enumerates.
        log.info("graph_snapshot_stale_after_load", org_id=organization_id)
        return snapshot
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
    accessible_teams: set[str] | None = None,
    accessible_delegations: set[str] | None = None,
) -> _ReaderCacheKey:
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
        tuple(sorted(str(key) for key in accessible_teams or ())),
        tuple(sorted(str(key) for key in accessible_delegations or ())),
    )


def _reader_visible_snapshot(
    snapshot: GraphSnapshot,
    *,
    principal_id: str | None,
    accessible_projects: set[str] | None,
    allowed_memory_scope_keys: set[str] | None = None,
    accessible_teams: set[str] | None = None,
    accessible_delegations: set[str] | None = None,
) -> GraphSnapshot:
    """Reduce a snapshot to the rows this reader is authorized to see.

    The scope predicate is expressible in SurrealQL against the flexible
    attributes object, but pushing it down would restate a policy whose
    branches (owner as principal_id or scope_key, unrecognized scopes denied,
    team and delegated scopes require persisted reader grants) already
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
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
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
    accessible_teams: set[str] | None = None,
    accessible_delegations: set[str] | None = None,
    max_entities: int | None = None,
    max_relationships: int | None = None,
) -> GraphSnapshot:
    snapshot_key: _SnapshotKey = (organization_id, max_entities, max_relationships)
    cache_key: _VisibleSnapshotKey = (
        *snapshot_key,
        _reader_cache_key(
            principal_id,
            accessible_projects,
            allowed_memory_scope_keys,
            accessible_teams,
            accessible_delegations,
        ),
    )
    cached = GRAPH_VISIBLE_SNAPSHOT_CACHE.get(cache_key)
    if cached is not None:
        base, visible = cached
        if _fresh_snapshot(GRAPH_SNAPSHOT_CACHE.get(snapshot_key)) is base:
            log.debug("graph_visible_snapshot_cache_hit", org_id=organization_id)
            return visible
        GRAPH_VISIBLE_SNAPSHOT_CACHE.pop(cache_key, None)

    return await _shared_load(
        GRAPH_VISIBLE_SNAPSHOT_LOADS,
        _GRAPH_VISIBLE_SNAPSHOT_WAITERS,
        cache_key,
        lambda: _load_visible_graph_snapshot(
            client,
            organization_id,
            cache_key=cache_key,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
            max_entities=max_entities,
            max_relationships=max_relationships,
        ),
        event="graph_visible_snapshot_load",
        organization_id=organization_id,
    )


async def _load_visible_graph_snapshot(
    client: Any,
    organization_id: str,
    *,
    cache_key: _VisibleSnapshotKey,
    principal_id: str | None,
    accessible_projects: set[str] | None,
    allowed_memory_scope_keys: set[str] | None,
    accessible_teams: set[str] | None,
    accessible_delegations: set[str] | None,
    max_entities: int | None,
    max_relationships: int | None,
) -> GraphSnapshot:
    base = await _get_graph_snapshot(
        client,
        organization_id,
        max_entities=max_entities,
        max_relationships=max_relationships,
    )
    proven = await _current_graph_snapshot(
        client,
        organization_id,
        base,
        source_visible=partial(
            graph_row_read_allowed,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
        ),
    )
    visible = _reader_visible_snapshot(
        _shared_rows(base, proven),
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    # Bind the fingerprint off the loop, so the derived caches never serialize
    # the whole graph again on a warm request.
    await asyncio.to_thread(_snapshot_fingerprint, visible)
    GRAPH_VISIBLE_SNAPSHOT_CACHE[cache_key] = (base, visible)
    log.info(
        "graph_visible_snapshot_cache_updated",
        org_id=organization_id,
        entity_count=len(visible.entities),
        relationship_count=len(visible.relationships),
    )
    return visible


async def _current_graph_entities(
    client: Any,
    organization_id: str,
    ids: list[str],
    *,
    source_visible: Callable[[Any], bool] | None = None,
) -> dict[str, Entity]:
    """Keep current-row and ancestry reads on the supplied graph owner."""
    from sibyl_core.services.graph_community_managers import _runtime_for_client
    from sibyl_core.services.graph_read_availability import available_graph_entities

    return await available_graph_entities(
        organization_id,
        ids,
        runtime=_runtime_for_client(client, organization_id),
        source_visible=source_visible,
        include_embeddings=False,
    )


async def _current_graph_relationships(
    client: Any, organization_id: str, ids: list[str]
) -> dict[str, Relationship]:
    """Validate current edge bodies and generations on the supplied graph."""
    from sibyl_core.services.graph_community_managers import _runtime_for_client
    from sibyl_core.services.graph_read_availability import available_graph_relationships

    return await available_graph_relationships(
        organization_id,
        ids,
        runtime=_runtime_for_client(client, organization_id),
    )


async def _current_graph_snapshot(
    client: Any,
    organization_id: str,
    snapshot: GraphSnapshot,
    *,
    source_visible: Callable[[Any], bool] | None = None,
) -> GraphSnapshot:
    """Refresh cached identities before their content enters a reader cache.

    Enumeration stays cached. Current rows and protected ancestry determine
    what can be rendered; a replacement using the same ID cannot revive an
    older cached label or relationship fact. One view pass proves edges and
    endpoints together: an edge whose stored body moved since enumeration is
    dropped here rather than refreshed, and the write that moved it already
    retired the enumeration it came from.
    """
    from sibyl_core.services.graph_community_managers import _runtime_for_client
    from sibyl_core.services.graph_view_availability import available_graph_view

    entities, current_relationships = await available_graph_view(
        organization_id,
        list(snapshot.entity_by_id),
        {relationship.id: relationship for relationship in snapshot.relationships},
        runtime=_runtime_for_client(client, organization_id),
        source_visible=source_visible,
    )
    relationships = [
        current_relationships[r.id] for r in snapshot.relationships if r.id in current_relationships
    ]
    return GraphSnapshot(
        entities=list(entities.values()),
        relationships=[
            relationship
            for relationship in relationships
            if relationship.source_id in entities and relationship.target_id in entities
        ],
        entity_by_id=entities,
    )


# Body text and vectors never reach a rendered node; the proofs above already
# ran on the full rows.
_BODY_METADATA_KEYS = frozenset({"content", "embedding", "name_embedding", "fact_embedding"})


def _compact_entity(entity: Entity) -> Entity:
    update: dict[str, Any] = {}
    if entity.content:
        update["content"] = ""
    if entity.embedding is not None:
        update["embedding"] = None
    if _BODY_METADATA_KEYS & entity.metadata.keys():
        update["metadata"] = {
            key: value for key, value in entity.metadata.items() if key not in _BODY_METADATA_KEYS
        }
    return entity.model_copy(update=update) if update else entity


def _shared_rows(base: GraphSnapshot, proven: GraphSnapshot) -> GraphSnapshot:
    """The proven ids, carried by the enumeration's own row objects.

    The proof reads fresh rows, with bodies, to decide what is current. What a
    reader renders from those rows is name, type, description and semantic
    metadata, which the enumeration already holds in the same generation; so
    every reader snapshot of an organization points at the enumeration's
    objects and costs its id lists, not a second copy of the graph.
    """
    if not base.relationship_by_id and base.relationships:
        base.relationship_by_id = {edge.id: edge for edge in base.relationships if edge.id}
    entities = [
        base.entity_by_id[identifier]
        for identifier in proven.entity_by_id
        if identifier in base.entity_by_id
    ]
    relationships = [
        base.relationship_by_id[edge.id]
        for edge in proven.relationships
        if edge.id in base.relationship_by_id
    ]
    return GraphSnapshot(
        entities=entities,
        relationships=relationships,
        entity_by_id=_entity_index(entities),
    )


def _snapshot_fingerprint(snapshot: GraphSnapshot) -> str:
    """Bind derived cache values to their current, authorized input content.

    Computed once per snapshot object and remembered on it: a snapshot's rows
    never change after it is built, and the derived caches ask on every call.
    """
    if snapshot.fingerprint is not None:
        return snapshot.fingerprint
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
    snapshot.fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return snapshot.fingerprint


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
