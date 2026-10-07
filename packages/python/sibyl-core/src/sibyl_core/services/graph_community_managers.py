"""Manager construction and paginated graph reads for community services."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_community_models import DetectedCommunity

if TYPE_CHECKING:
    from sibyl_core.services.graph_runtime import GraphRuntime


type _ManagerFactory = Callable[[Any, str], Any]

_entity_manager_factory: _ManagerFactory | None = None
_relationship_manager_factory: _ManagerFactory | None = None
_COMMUNITY_PAGE_SIZE = 500


def _entity_summary(entity: Entity) -> str:
    summary = entity.metadata.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    return entity.description or ""


def _community_name(community: DetectedCommunity) -> str:
    return f"Community L{community.level} ({community.member_count} members)"


def _community_metadata(entity: Entity) -> dict[str, Any]:
    return entity.metadata if isinstance(entity.metadata, dict) else {}


def _community_level(entity: Entity) -> int:
    level = _community_metadata(entity).get("level")
    return level if isinstance(level, int) else 0


def _community_member_count(entity: Entity) -> int:
    member_count = _community_metadata(entity).get("member_count")
    return member_count if isinstance(member_count, int) else 0


def _build_community_entity(community: DetectedCommunity, *, created_at: datetime) -> Entity:
    summary = ""
    return Entity(
        id=community.id,
        entity_type=EntityType.COMMUNITY,
        name=_community_name(community),
        description=summary,
        content=summary,
        created_at=created_at,
        metadata={
            "member_ids": list(community.member_ids),
            "member_count": community.member_count,
            "level": community.level,
            "resolution": community.resolution,
            "modularity": community.modularity,
            "parent_community_id": community.parent_id,
            "child_community_ids": list(community.child_ids),
            "summary": summary,
        },
    )


async def _list_community_entities(
    entity_manager: Any,
) -> list[Entity]:
    communities: list[Entity] = []
    offset = 0

    while True:
        kwargs: dict[str, Any] = {
            "limit": _COMMUNITY_PAGE_SIZE,
            "offset": offset,
            "include_archived": True,
        }
        if getattr(entity_manager, "supports_lightweight_entity_list", False):
            kwargs["include_content"] = False
        batch = await entity_manager.list_by_type(EntityType.COMMUNITY, **kwargs)
        if not batch:
            break
        communities.extend(batch)
        if len(batch) < _COMMUNITY_PAGE_SIZE:
            break
        offset += _COMMUNITY_PAGE_SIZE

    return communities


def _attached_manager(client: Any, name: str) -> Any | None:
    try:
        client_state = vars(client)
    except TypeError:
        return None
    manager = client_state.get(name)
    return manager if manager is not None else None


def _entity_manager_for_client(client: Any, organization_id: str) -> Any:
    from sibyl_core.services.graph_client import SurrealGraphClient
    from sibyl_core.services.graph_entities import EntityManager

    if _entity_manager_factory is not None:
        return _entity_manager_factory(client, organization_id)

    if isinstance(client, SurrealGraphClient):
        return EntityManager(client, group_id=organization_id)

    manager = _attached_manager(client, "entity_manager")
    if manager is not None:
        return manager

    raise RuntimeError(
        "Community graph operations require a native graph client or attached entity_manager"
    )


def _relationship_manager_for_client(client: Any, organization_id: str) -> Any:
    from sibyl_core.services.graph_client import SurrealGraphClient
    from sibyl_core.services.graph_relationships import RelationshipManager

    if _relationship_manager_factory is not None:
        return _relationship_manager_factory(client, organization_id)

    if isinstance(client, SurrealGraphClient):
        return RelationshipManager(client, group_id=organization_id)

    manager = _attached_manager(client, "relationship_manager")
    if manager is not None:
        return manager

    raise RuntimeError(
        "Community graph operations require a native graph client or attached relationship_manager"
    )


def _runtime_for_client(client: Any, organization_id: str) -> GraphRuntime:
    from sibyl_core.services.graph_runtime import GraphRuntime

    return GraphRuntime(
        client=client,
        entity_manager=_entity_manager_for_client(client, organization_id),
        relationship_manager=_relationship_manager_for_client(client, organization_id),
    )


_ENUMERATION_PAGE_SIZE = 1000
_ENTITY_ORDER = "ORDER BY updated_at DESC, created_at DESC, uuid DESC"
_RELATIONSHIP_ORDER = "ORDER BY created_at DESC, uuid DESC"
# Vector columns are the bulk of an edge and never render. Everything else is
# projected exactly as the validation snapshot reads it, so an enumerated edge
# decodes to the same Relationship as its refreshed counterpart and the view
# comparison sees one body rather than two projections of it.
_RELATIONSHIP_ENUMERATION_FIELDS = (
    "*, in.uuid AS source_uuid, out.uuid AS target_uuid, "
    "<string>created_at AS created_at_text "
    "OMIT fact_embedding, attributes.fact_embedding, attributes.embedding"
)
# Keyset cursors travel as the stored ISO text. A datetime that round-trips
# through the SDK keeps only microseconds, and a bound truncated that way
# skips every row inside the dropped nanoseconds. The embedded 2.x engine
# refuses to cast an undated row's NONE; its text is never used as a cursor.
_ENTITY_CURSOR_FIELDS = (
    "<string>(updated_at ?? created_at) AS updated_at_text, <string>created_at AS created_at_text"
)


def _native_graph_client(client: Any) -> bool:
    from sibyl_core.services.graph_client import SurrealGraphClient

    return isinstance(client, SurrealGraphClient)


def _cursor_binding(client: Any) -> tuple[str, Callable[[str], object]]:
    """How a cursor's ISO text reaches the engine: a bound template and a value.

    The server seeks the composite index for a native datetime parameter, so
    the text is sent as one through the SDK's Datetime wrapper, which keeps
    its nanoseconds. The embedded 2.x engine returns the oldest rows for a
    DESC LIMIT page bounded by a datetime parameter; an inline cast of the
    text keeps it off that path, and it has no planner to lose.
    """
    from surrealdb.data.types.datetime import Datetime

    from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url

    if is_embedded_surreal_url(client._url):
        return "<datetime>${}", str
    return "${}", Datetime


def _page_limit(collected: int, *, batch_size: int, max_items: int | None) -> int:
    limit = min(max(int(batch_size), 1), _ENUMERATION_PAGE_SIZE)
    if max_items is not None:
        limit = min(limit, max(max_items - collected, 0))
    return limit


async def _page_rows(
    client: Any, selects: list[str], params: dict[str, Any]
) -> list[dict[str, Any]]:
    """One round trip per page: the cursor's tie seek, then the strict range.

    Two top-level statements in one batch, each planned against the bound
    cursor parameter; the client returns their results in statement order.
    """
    from sibyl_core.services.graph_common import normalize_graph_records

    if len(selects) == 1:
        return normalize_graph_records(await client.execute_query(selects[0], **params))
    results = await client.execute_query_batch(" ".join(selects), **params)
    if not isinstance(results, list) or len(results) != len(selects):
        raise RuntimeError("graph enumeration page returned no statement results")
    return [row for result in results for row in normalize_graph_records(result)]


async def _list_all_entities(
    client: Any,
    organization_id: str,
    *,
    batch_size: int = 1000,
    max_items: int | None = None,
) -> list[Entity]:
    if _entity_manager_factory is None and _native_graph_client(client):
        return await _walk_entities(
            client, organization_id, batch_size=batch_size, max_items=max_items
        )

    manager = _entity_manager_for_client(client, organization_id)
    entities: list[Entity] = []
    offset = 0

    while True:
        if max_items is not None and len(entities) >= max_items:
            break
        page_limit = batch_size
        if max_items is not None:
            page_limit = min(page_limit, max(max_items - len(entities), 0))
        if page_limit <= 0:
            break
        kwargs: dict[str, Any] = {
            "limit": page_limit,
            "offset": offset,
            "include_archived": True,
        }
        if getattr(manager, "supports_lightweight_entity_list", False):
            kwargs["include_content"] = False
        batch = await manager.list_all(**kwargs)
        if not batch:
            break
        entities.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit

    return entities


async def _walk_entities(
    client: Any,
    organization_id: str,
    *,
    batch_size: int,
    max_items: int | None,
) -> list[Entity]:
    """Enumerate every entity by keyset over the (updated_at, created_at, uuid) index.

    An offset page costs the scan of every page before it; a keyset page seeks
    the index at its cursor. The cursor's own timestamp is drained with an
    equality seek before the strict range continues below it: a single
    "<= cursor AND NOT (...)" predicate is index-served too, but the 3.x
    planner drops the rows sharing the cursor's timestamp once it has consumed
    that bound for the range. Rows without updated_at sort after every dated
    row and form a second phase with its own range.
    """
    from sibyl_core.services.graph_records import _ENTITY_LIST_FIELDS, _entity_from_row

    # The list projection, plus the cursor text; its OMIT list stays the
    # single source of what an enumerated row leaves behind.
    fields = f"*, {_ENTITY_CURSOR_FIELDS} {_ENTITY_LIST_FIELDS.removeprefix('*').strip()}"
    select = f"SELECT {fields} FROM entity WHERE group_id = $group_id"
    bound, cursor_value = _cursor_binding(client)
    updated_bound, created_bound = bound.format("updated_at"), bound.format("created_at")
    before_cursor = (
        f"(created_at < {created_bound} OR (created_at = {created_bound} AND uuid < $uuid))"
    )
    entities: list[Entity] = []
    dated = True
    cursor: dict[str, Any] | None = None

    while True:
        limit = _page_limit(len(entities), batch_size=batch_size, max_items=max_items)
        if limit <= 0:
            break
        params: dict[str, Any] = {"group_id": organization_id, "limit": limit, **(cursor or {})}
        if dated and cursor is None:
            selects = [f"{select} {_ENTITY_ORDER} LIMIT $limit;"]
        elif dated:
            selects = [
                f"{select} AND updated_at = {updated_bound} AND {before_cursor} "
                f"{_ENTITY_ORDER} LIMIT $limit;",
                f"{select} AND updated_at < {updated_bound} {_ENTITY_ORDER} LIMIT $limit;",
            ]
        elif cursor is None:
            selects = [f"{select} AND updated_at IS NONE {_ENTITY_ORDER} LIMIT $limit;"]
        else:
            selects = [
                f"{select} AND updated_at IS NONE AND {before_cursor} {_ENTITY_ORDER} LIMIT $limit;"
            ]
        rows = (await _page_rows(client, selects, params))[:limit]
        entities.extend(_entity_from_row(row) for row in rows)
        if not rows:
            if not dated:
                break
            dated, cursor = False, None
            continue
        last = rows[-1]
        if dated and last.get("updated_at") is None:
            dated = False
        elif len(rows) < limit:
            if not dated:
                break
            dated, cursor = False, None
            continue
        cursor = {
            "created_at": cursor_value(str(last.get("created_at_text"))),
            "uuid": last.get("uuid"),
        }
        if dated:
            cursor["updated_at"] = cursor_value(str(last.get("updated_at_text")))

    return entities


async def _list_all_relationships(
    client: Any,
    organization_id: str,
    *,
    batch_size: int = 1000,
    max_items: int | None = None,
    relationship_types: list[RelationshipType] | None = None,
) -> list[Relationship]:
    if _relationship_manager_factory is None and _native_graph_client(client):
        return await _walk_relationships(
            client,
            organization_id,
            batch_size=batch_size,
            max_items=max_items,
            relationship_types=relationship_types,
        )

    manager = _relationship_manager_for_client(client, organization_id)
    relationships: list[Relationship] = []
    offset = 0

    while True:
        if max_items is not None and len(relationships) >= max_items:
            break
        page_limit = batch_size
        if max_items is not None:
            page_limit = min(page_limit, max(max_items - len(relationships), 0))
        if page_limit <= 0:
            break
        batch = await manager.list_all(
            relationship_types=relationship_types,
            limit=page_limit,
            offset=offset,
        )
        if not batch:
            break
        relationships.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit

    return relationships


async def _walk_relationships(
    client: Any,
    organization_id: str,
    *,
    batch_size: int,
    max_items: int | None,
    relationship_types: list[RelationshipType] | None,
) -> list[Relationship]:
    """Enumerate every edge by keyset over the (created_at, uuid) index.

    The readable legacy metadata encodings are captured once for the whole
    walk; the per-page query it used to precede is a full decode of every
    edge, and the walk reads the same rows that capture guards.
    """
    from sibyl_core.services.graph_records import (
        readable_legacy_relationship_metadata,
        readable_relationship_from_surreal_row,
        relationship_weight_predicate,
    )

    type_values = [rel_type.value for rel_type in relationship_types or ()]
    type_clause = "AND name IN $relationship_types" if type_values else ""
    readable_metadata = await readable_legacy_relationship_metadata(
        client,
        f"group_id = $group_id {type_clause}",
        group_id=organization_id,
        relationship_types=type_values,
    )
    select = (
        f"SELECT {_RELATIONSHIP_ENUMERATION_FIELDS} FROM relates_to "
        f"WHERE group_id = $group_id {type_clause} {relationship_weight_predicate(client)}"
    )
    bound, cursor_value = _cursor_binding(client)
    created_bound = bound.format("created_at")
    relationships: list[Relationship] = []
    cursor: dict[str, Any] | None = None

    while True:
        limit = _page_limit(len(relationships), batch_size=batch_size, max_items=max_items)
        if limit <= 0:
            break
        params: dict[str, Any] = {
            "group_id": organization_id,
            "limit": limit,
            "relationship_types": type_values,
            "readable_relationship_metadata": readable_metadata,
            "nan_relationship_weight": float("nan"),
            **(cursor or {}),
        }
        if cursor is None:
            selects = [f"{select} {_RELATIONSHIP_ORDER} LIMIT $limit;"]
        else:
            selects = [
                f"{select} AND created_at = {created_bound} AND uuid < $uuid "
                f"{_RELATIONSHIP_ORDER} LIMIT $limit;",
                f"{select} AND created_at < {created_bound} {_RELATIONSHIP_ORDER} LIMIT $limit;",
            ]
        rows = (await _page_rows(client, selects, params))[:limit]
        relationships.extend(
            relationship
            for row in rows
            if (relationship := readable_relationship_from_surreal_row(row)) is not None
        )
        if len(rows) < limit:
            break
        cursor = {
            "created_at": cursor_value(str(rows[-1].get("created_at_text"))),
            "uuid": rows[-1].get("uuid"),
        }

    return relationships
