"""Manager construction and paginated graph reads for community services."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_community_models import DetectedCommunity

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


async def _list_all_entities(
    client: Any,
    organization_id: str,
    *,
    batch_size: int = 1000,
    max_items: int | None = None,
) -> list[Entity]:
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


async def _list_all_relationships(
    client: Any,
    organization_id: str,
    *,
    batch_size: int = 1000,
    max_items: int | None = None,
    relationship_types: list[RelationshipType] | None = None,
) -> list[Relationship]:
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
