"""Discovery tools for the Conventions MCP Server.

Provides exploration and listing capabilities for the knowledge graph.
"""

from dataclasses import dataclass, field

import structlog

from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.models.entities import EntityType, RelationshipType

log = structlog.get_logger()


@dataclass
class EntitySummary:
    """Summary of an entity for listing."""

    id: str
    name: str
    entity_type: str
    description: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class RelatedEntity:
    """An entity related to another through the graph."""

    id: str
    name: str
    entity_type: str
    relationship_type: str
    relationship_direction: str  # "outgoing" or "incoming"
    distance: int  # Hops from source


@dataclass
class ListResult:
    """Result of a list operation."""

    entity_type: str
    items: list[EntitySummary]
    total_count: int
    filters_applied: dict[str, str]


async def list_patterns(
    category: str | None = None,
    language: str | None = None,
    limit: int = 50,
) -> ListResult:
    """List all available patterns.

    Args:
        category: Filter by category.
        language: Filter by programming language.
        limit: Maximum results.

    Returns:
        ListResult with pattern summaries.
    """
    log.info("Listing patterns", category=category, language=language)

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        patterns = await entity_manager.list_by_type(EntityType.PATTERN, limit=limit * 2)

        items = []
        filters = {}
        if category:
            filters["category"] = category
        if language:
            filters["language"] = language

        for pattern in patterns:
            # Apply filters
            if category:
                pat_category = getattr(pattern, "category", "") or ""
                if category.lower() not in pat_category.lower():
                    continue

            if language:
                pat_languages = getattr(pattern, "languages", []) or []
                if language.lower() not in [l.lower() for l in pat_languages]:
                    continue

            items.append(
                EntitySummary(
                    id=pattern.id,
                    name=pattern.name,
                    entity_type=pattern.entity_type.value,
                    description=pattern.description[:200] if pattern.description else "",
                    metadata={
                        "category": getattr(pattern, "category", None),
                        "languages": getattr(pattern, "languages", []),
                    },
                )
            )

            if len(items) >= limit:
                break

        return ListResult(
            entity_type="pattern",
            items=items,
            total_count=len(items),
            filters_applied=filters,
        )

    except Exception as e:
        log.warning("Failed to list patterns", error=str(e))
        return ListResult(entity_type="pattern", items=[], total_count=0, filters_applied={})


async def list_templates(
    template_type: str | None = None,
    language: str | None = None,
    limit: int = 50,
) -> ListResult:
    """List all available templates.

    Args:
        template_type: Filter by type (code, config, project, workflow).
        language: Filter by programming language.
        limit: Maximum results.

    Returns:
        ListResult with template summaries.
    """
    log.info("Listing templates", template_type=template_type, language=language)

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        templates = await entity_manager.list_by_type(EntityType.TEMPLATE, limit=limit * 2)

        items = []
        filters = {}
        if template_type:
            filters["template_type"] = template_type
        if language:
            filters["language"] = language

        for template in templates:
            # Apply filters
            if template_type:
                tpl_type = getattr(template, "template_type", "") or ""
                if template_type.lower() != tpl_type.lower():
                    continue

            if language:
                tpl_lang = getattr(template, "language", "") or ""
                if language.lower() not in tpl_lang.lower():
                    continue

            items.append(
                EntitySummary(
                    id=template.id,
                    name=template.name,
                    entity_type=template.entity_type.value,
                    description=template.description[:200] if template.description else "",
                    metadata={
                        "template_type": getattr(template, "template_type", None),
                        "language": getattr(template, "language", None),
                        "file_path": template.source_file,
                    },
                )
            )

            if len(items) >= limit:
                break

        return ListResult(
            entity_type="template",
            items=items,
            total_count=len(items),
            filters_applied=filters,
        )

    except Exception as e:
        log.warning("Failed to list templates", error=str(e))
        return ListResult(entity_type="template", items=[], total_count=0, filters_applied={})


async def list_rules(
    severity: str | None = None,
    language: str | None = None,
    limit: int = 50,
) -> ListResult:
    """List all sacred rules.

    Args:
        severity: Filter by severity (error, warning, info).
        language: Filter by programming language.
        limit: Maximum results.

    Returns:
        ListResult with rule summaries.
    """
    log.info("Listing rules", severity=severity, language=language)

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        rules = await entity_manager.list_by_type(EntityType.RULE, limit=limit * 2)

        items = []
        filters = {}
        if severity:
            filters["severity"] = severity
        if language:
            filters["language"] = language

        for rule in rules:
            # Apply filters
            if severity:
                rule_sev = getattr(rule, "severity", "error")
                if severity.lower() != rule_sev.lower():
                    continue

            if language:
                rule_langs = getattr(rule, "languages", []) or []
                if language.lower() not in [l.lower() for l in rule_langs]:
                    continue

            items.append(
                EntitySummary(
                    id=rule.id,
                    name=rule.name,
                    entity_type=rule.entity_type.value,
                    description=rule.description[:200] if rule.description else "",
                    metadata={
                        "severity": getattr(rule, "severity", "error"),
                        "enforcement": getattr(rule, "enforcement", None),
                    },
                )
            )

            if len(items) >= limit:
                break

        return ListResult(
            entity_type="rule",
            items=items,
            total_count=len(items),
            filters_applied=filters,
        )

    except Exception as e:
        log.warning("Failed to list rules", error=str(e))
        return ListResult(entity_type="rule", items=[], total_count=0, filters_applied={})


async def list_topics(
    parent: str | None = None,
    limit: int = 50,
) -> ListResult:
    """List all knowledge topics.

    Args:
        parent: Filter by parent topic.
        limit: Maximum results.

    Returns:
        ListResult with topic summaries.
    """
    log.info("Listing topics", parent=parent)

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        topics = await entity_manager.list_by_type(EntityType.TOPIC, limit=limit * 2)

        items = []
        filters = {}
        if parent:
            filters["parent"] = parent

        for topic in topics:
            # Apply parent filter
            if parent:
                topic_parent = getattr(topic, "parent_topic", None)
                if not topic_parent or parent.lower() not in topic_parent.lower():
                    continue

            items.append(
                EntitySummary(
                    id=topic.id,
                    name=topic.name,
                    entity_type=topic.entity_type.value,
                    description=topic.description[:200] if topic.description else "",
                    metadata={
                        "parent_topic": getattr(topic, "parent_topic", None),
                    },
                )
            )

            if len(items) >= limit:
                break

        return ListResult(
            entity_type="topic",
            items=items,
            total_count=len(items),
            filters_applied=filters,
        )

    except Exception as e:
        log.warning("Failed to list topics", error=str(e))
        return ListResult(entity_type="topic", items=[], total_count=0, filters_applied={})


async def get_related(
    entity_id: str,
    relationship_types: list[str] | None = None,
    depth: int = 1,
    limit: int = 20,
) -> list[RelatedEntity]:
    """Get entities related to a specific entity.

    Traverses the knowledge graph to find connected entities.

    Args:
        entity_id: ID of the entity to find relations for.
        relationship_types: Filter by relationship types.
        depth: How many hops to traverse (1-3).
        limit: Maximum results.

    Returns:
        List of related entities with relationship info.
    """
    log.info("Getting related entities", entity_id=entity_id, depth=depth)

    # Clamp depth to safe range
    depth = max(1, min(depth, 3))

    try:
        client = await get_graph_client()
        relationship_manager = RelationshipManager(client)

        # Convert string types to enum if provided
        rel_types = None
        if relationship_types:
            rel_types = []
            for rt in relationship_types:
                try:
                    rel_types.append(RelationshipType(rt.upper()))
                except ValueError:
                    log.warning("Unknown relationship type", type=rt)

        # Get related entities
        results = await relationship_manager.get_related_entities(
            entity_id=entity_id,
            relationship_types=rel_types,
            depth=depth,
            limit=limit,
        )

        related = []
        for entity, relationship in results:
            # Determine direction
            if relationship.source_id == entity_id:
                direction = "outgoing"
            else:
                direction = "incoming"

            related.append(
                RelatedEntity(
                    id=entity.id,
                    name=entity.name,
                    entity_type=entity.entity_type.value,
                    relationship_type=relationship.relationship_type.value,
                    relationship_direction=direction,
                    distance=1,  # Simplified - would need path info for multi-hop
                )
            )

        return related

    except Exception as e:
        log.warning("Failed to get related entities", error=str(e), entity_id=entity_id)
        return []
