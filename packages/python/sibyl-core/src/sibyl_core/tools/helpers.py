"""Helper functions and constants for Sibyl MCP tools."""

import hashlib
import json
from typing import TYPE_CHECKING, Any

import structlog

from sibyl_core.models.entities import EntityType

if TYPE_CHECKING:
    from sibyl_core.graph.client import GraphClient
    from sibyl_core.graph.entities import EntityManager

log = structlog.get_logger()

# Valid entity types for filtering
VALID_ENTITY_TYPES = {t.value for t in EntityType}

# Validation constants
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 50000


def _get_field(entity: Any, field: str, default: Any = None) -> Any:
    """Get field from entity object or its metadata, with fallback default."""
    value = getattr(entity, field, None)
    if value is None:
        value = entity.metadata.get(field, default)
    return value if value is not None else default


def _serialize_enum(value: Any) -> Any:
    """Serialize enum value to its string representation."""
    if value is None:
        return None
    return value.value if hasattr(value, "value") else value


def _build_entity_metadata(entity: Any) -> dict[str, Any]:
    """Build standardized metadata dict from entity with common fields."""
    status = _serialize_enum(_get_field(entity, "status"))
    priority = _serialize_enum(_get_field(entity, "priority"))

    extra = {
        "category": _get_field(entity, "category"),
        "languages": _get_field(entity, "languages"),
        "severity": _get_field(entity, "severity"),
        "template_type": _get_field(entity, "template_type"),
        "status": status,
        "priority": priority,
        "project_id": _get_field(entity, "project_id"),
        "assignees": _get_field(entity, "assignees"),
    }
    return {**entity.metadata, **{k: v for k, v in extra.items() if v is not None}}


def _generate_id(prefix: str, *parts: str) -> str:
    """Generate a deterministic entity ID."""
    combined = ":".join(str(p)[:100] for p in parts)
    hash_bytes = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return f"{prefix}_{hash_bytes}"


# =============================================================================
# Auto-Tagging System
# =============================================================================

# Domain keywords for auto-tagging
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "frontend": [
        "ui",
        "ux",
        "component",
        "react",
        "vue",
        "angular",
        "css",
        "style",
        "layout",
        "responsive",
        "animation",
        "button",
        "modal",
        "form",
        "input",
        "page",
        "view",
        "render",
        "display",
        "browser",
        "dom",
        "jsx",
        "tsx",
        "tailwind",
        "styled",
        "theme",
        "dark mode",
        "light mode",
    ],
    "backend": [
        "api",
        "server",
        "endpoint",
        "route",
        "handler",
        "controller",
        "service",
        "middleware",
        "database",
        "query",
        "model",
        "schema",
        "auth",
        "jwt",
        "token",
        "session",
        "fastapi",
        "flask",
        "django",
        "express",
        "graphql",
        "rest",
        "crud",
    ],
    "database": [
        "database",
        "db",
        "sql",
        "postgres",
        "mysql",
        "mongodb",
        "redis",
        "migration",
        "schema",
        "query",
        "index",
        "table",
        "collection",
        "falkordb",
        "graph",
        "cypher",
        "neo4j",
        "supabase",
    ],
    "devops": [
        "deploy",
        "docker",
        "kubernetes",
        "k8s",
        "ci",
        "cd",
        "pipeline",
        "github actions",
        "terraform",
        "aws",
        "gcp",
        "azure",
        "cloud",
        "nginx",
        "load balancer",
        "scaling",
        "monitoring",
    ],
    "testing": [
        "test",
        "spec",
        "pytest",
        "jest",
        "vitest",
        "unit test",
        "e2e",
        "integration",
        "mock",
        "fixture",
        "coverage",
        "tdd",
        "assertion",
    ],
    "docs": [
        "documentation",
        "readme",
        "docs",
        "comment",
        "docstring",
        "jsdoc",
        "api docs",
        "guide",
        "tutorial",
        "example",
    ],
    "security": [
        "security",
        "auth",
        "authentication",
        "authorization",
        "permission",
        "role",
        "acl",
        "xss",
        "csrf",
        "injection",
        "encrypt",
        "hash",
        "password",
        "secret",
        "vulnerability",
    ],
    "performance": [
        "performance",
        "optimize",
        "cache",
        "lazy",
        "memoize",
        "bundle",
        "minify",
        "compress",
        "speed",
        "latency",
        "profil",
        "benchmark",
    ],
}

# Task type keywords
_TYPE_KEYWORDS: dict[str, list[str]] = {
    "feature": ["add", "implement", "create", "build", "new", "introduce", "support"],
    "bug": ["fix", "bug", "issue", "error", "broken", "crash", "fail", "wrong"],
    "refactor": [
        "refactor",
        "clean",
        "reorganize",
        "restructure",
        "simplify",
        "improve",
        "extract",
        "consolidate",
        "dedup",
    ],
    "chore": [
        "update",
        "upgrade",
        "bump",
        "dependency",
        "deps",
        "config",
        "setup",
        "maintenance",
        "housekeeping",
    ],
    "research": [
        "research",
        "investigate",
        "explore",
        "spike",
        "poc",
        "prototype",
        "experiment",
        "evaluate",
        "compare",
    ],
}


def auto_tag_task(
    title: str,
    description: str,
    technologies: list[str] | None = None,
    domain: str | None = None,
    explicit_tags: list[str] | None = None,
    project_tags: list[str] | None = None,
) -> list[str]:
    """Generate tags automatically based on task content.

    Analyzes title, description, technologies, and domain to generate
    relevant tags. Prefers existing project tags when applicable.

    Args:
        title: Task title
        description: Task description
        technologies: List of technologies
        domain: Knowledge domain
        explicit_tags: Manually specified tags
        project_tags: Existing tags from project's tasks (for consistency)

    Returns:
        Deduplicated list of tags
    """
    tags: set[str] = set()
    text = f"{title} {description}".lower()

    # Normalize project tags for matching
    existing_tags = {t.lower().strip(): t for t in (project_tags or [])}

    # Start with explicit tags (normalized to lowercase)
    if explicit_tags:
        tags.update(t.lower().strip() for t in explicit_tags if t.strip())

    # Add domain as a tag if provided
    if domain:
        tags.add(domain.lower().strip())

    # Add technology-derived tags
    if technologies:
        for tech in technologies:
            normalized = tech.lower().strip()
            # Common tech name mappings to domains
            tech_tag = {
                "react": "frontend",
                "vue": "frontend",
                "angular": "frontend",
                "next.js": "frontend",
                "nextjs": "frontend",
                "tailwind": "frontend",
                "python": "backend",
                "fastapi": "backend",
                "django": "backend",
                "flask": "backend",
                "typescript": "typescript",
                "javascript": "javascript",
                "postgres": "database",
                "postgresql": "database",
                "mongodb": "database",
                "redis": "database",
                "docker": "devops",
                "kubernetes": "devops",
            }.get(normalized)
            if tech_tag:
                tags.add(tech_tag)
            # Also add the tech itself as a tag if it's short enough
            if len(normalized) <= 15:
                tags.add(normalized)

    # Check existing project tags first - prefer consistency
    for existing_lower in existing_tags:
        # Check if existing tag appears in text
        if existing_lower in text:
            tags.add(existing_lower)

    # Match domain keywords (only if not already matched from project tags)
    for tag, keywords in _DOMAIN_KEYWORDS.items():
        if tag not in tags:
            for keyword in keywords:
                if keyword in text:
                    tags.add(tag)
                    break

    # Match type keywords (only add the first match)
    type_tag = None
    for tag, keywords in _TYPE_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                type_tag = tag
                break
        if type_tag:
            break
    if type_tag:
        tags.add(type_tag)

    # Clean and sort tags
    return sorted(t for t in tags if t and len(t) >= 2)


async def get_project_tags(client: "GraphClient", project_id: str) -> list[str]:
    """Fetch all unique tags from a project's existing tasks.

    Args:
        client: Graph client instance
        project_id: Project ID to get tags from

    Returns:
        List of unique tags used in the project
    """
    from sibyl_core.graph.client import GraphClient

    try:
        # Query existing tasks in this project for their tags
        result = await client.driver.execute_query(
            """
            MATCH (n)
            WHERE (n:Episodic OR n:Entity)
              AND n.entity_type = 'task'
              AND n.project_id = $project_id
              AND n.tags IS NOT NULL
            RETURN DISTINCT n.tags as tags
            """,
            project_id=project_id,
        )

        # Normalize FalkorDB result (returns tuple, not object with result_set)
        rows = GraphClient.normalize_result(result)

        all_tags: set[str] = set()
        for row in rows:
            # Row is a list, tags is first element
            tags = row[0] if isinstance(row, list | tuple) else row.get("tags")
            if isinstance(tags, list):
                all_tags.update(t.lower() for t in tags if isinstance(t, str))
            elif isinstance(tags, str):
                # Handle JSON encoded list
                try:
                    parsed = json.loads(tags)
                    if isinstance(parsed, list):
                        all_tags.update(t.lower() for t in parsed if isinstance(t, str))
                except (json.JSONDecodeError, TypeError):
                    pass

        return sorted(all_tags)
    except Exception as e:
        log.debug("Failed to fetch project tags", error=str(e))
        return []


async def _auto_discover_links(
    entity_manager: "EntityManager",
    title: str,
    content: str,
    technologies: list[str],
    category: str | None,
    exclude_id: str,
    threshold: float = 0.75,
    limit: int = 5,
) -> list[tuple[str, float]]:
    """Discover related entities for auto-linking.

    Searches for patterns, rules, templates, and topics that are
    semantically similar to the new entity.

    Args:
        entity_manager: Entity manager for search.
        title: Entity title.
        content: Entity content.
        technologies: Technologies to include in search.
        category: Category/domain for filtering.
        exclude_id: ID to exclude from results (the new entity).
        threshold: Minimum similarity score (0-1).
        limit: Maximum links to discover.

    Returns:
        List of (entity_id, score) tuples above threshold.
    """
    # Build search query from title, content summary, and technologies
    tech_str = ", ".join(technologies[:5]) if technologies else ""
    query_parts = [title]
    if content:
        # Take first 200 chars of content
        query_parts.append(content[:200])
    if tech_str:
        query_parts.append(tech_str)
    if category:
        query_parts.append(category)

    query = " ".join(query_parts)

    # Search for linkable entity types
    linkable_types = [
        EntityType.PATTERN,
        EntityType.RULE,
        EntityType.TEMPLATE,
        EntityType.CONVENTION,
        EntityType.TOPIC,
    ]

    try:
        results = await entity_manager.search(
            query=query,
            entity_types=linkable_types,
            limit=limit * 2,  # Over-fetch to filter by threshold
        )

        # Filter by threshold and exclude self
        links: list[tuple[str, float]] = []
        for entity, score in results:
            if entity.id == exclude_id:
                continue
            if score >= threshold:
                links.append((entity.id, score))
            if len(links) >= limit:
                break

        return links

    except Exception as e:
        log.warning("auto_discover_search_failed", error=str(e))
        return []
