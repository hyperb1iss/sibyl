"""Community persistence and membership queries."""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.models.entities import EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_community_managers import (
    _build_community_entity,
    _community_level,
    _community_member_count,
    _entity_manager_for_client,
    _entity_summary,
    _list_community_entities,
    _relationship_manager_for_client,
)
from sibyl_core.services.graph_community_models import DetectedCommunity

log = structlog.get_logger()


async def store_communities(
    client: Any,
    organization_id: str,
    communities: list[DetectedCommunity],
    clear_existing: bool = True,
) -> int:
    """Store detected communities in the graph.

    Args:
        client: Graph client.
        communities: Communities to store.
        clear_existing: Whether to clear existing communities first.

    Returns:
        Number of communities stored.
    """
    if not communities:
        return 0

    log.info("store_communities_start", count=len(communities), clear_existing=clear_existing)
    entity_manager = _entity_manager_for_client(client, organization_id)
    relationship_manager = _relationship_manager_for_client(client, organization_id)

    # Clear existing communities if requested
    if clear_existing:
        try:
            for community in await _list_community_entities(entity_manager):
                with contextlib.suppress(Exception):
                    await entity_manager.delete(community.id)
        except Exception as e:
            log.warning("clear_communities_failed", error=str(e))

    # Store each community
    stored = 0
    now = datetime.now(UTC)

    for community in communities:
        try:
            await entity_manager.create(_build_community_entity(community, created_at=now))
            stored += 1
        except Exception as e:
            log.warning("store_community_failed", community_id=community.id, error=str(e))

    # Create BELONGS_TO relationships from members to communities
    membership_edges = [
        Relationship(
            id=str(uuid.uuid4()),
            source_id=member_id,
            target_id=community.id,
            relationship_type=RelationshipType.BELONGS_TO,
        )
        for community in communities
        for member_id in community.member_ids
    ]
    if membership_edges:
        with contextlib.suppress(Exception):
            await relationship_manager.create_bulk(membership_edges)

    log.info("store_communities_complete", stored=stored)
    return stored


async def get_entity_communities(
    client: Any,
    organization_id: str,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Get communities that an entity belongs to.

    Args:
        client: Graph client.
        entity_id: Entity UUID.

    Returns:
        List of community info dicts.
    """
    communities: list[dict[str, Any]] = []
    entity_manager = _entity_manager_for_client(client, organization_id)
    relationship_manager = _relationship_manager_for_client(client, organization_id)

    try:
        relationships = await relationship_manager.get_for_entity(
            entity_id,
            [RelationshipType.BELONGS_TO],
            direction="outgoing",
        )

        for relationship in relationships:
            with contextlib.suppress(Exception):
                community = await entity_manager.get(relationship.target_id)
                if community.entity_type != EntityType.COMMUNITY:
                    continue
                communities.append(
                    {
                        "id": community.id,
                        "name": community.name,
                        "level": _community_level(community),
                        "member_count": _community_member_count(community),
                        "summary": _entity_summary(community),
                    }
                )

    except Exception as e:
        log.warning("get_entity_communities_failed", entity_id=entity_id, error=str(e))

    communities.sort(key=lambda community: community["level"])
    return communities


async def get_community_members(
    client: Any,
    organization_id: str,
    community_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get members of a community.

    Args:
        client: Graph client.
        community_id: Community UUID.
        limit: Maximum members to return.

    Returns:
        List of member entity info.
    """
    members: list[dict[str, Any]] = []
    entity_manager = _entity_manager_for_client(client, organization_id)
    relationship_manager = _relationship_manager_for_client(client, organization_id)

    try:
        relationships = await relationship_manager.get_for_entity(
            community_id,
            [RelationshipType.BELONGS_TO],
            direction="incoming",
        )

        for relationship in relationships[:limit]:
            with contextlib.suppress(Exception):
                member = await entity_manager.get(relationship.source_id)
                members.append(
                    {
                        "id": member.id,
                        "name": member.name,
                        "type": member.entity_type.value,
                        "description": member.description,
                    }
                )

    except Exception as e:
        log.warning("get_community_members_failed", community_id=community_id, error=str(e))

    return members
