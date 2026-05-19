"""Helper functions and constants for Sibyl MCP tools."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import structlog

from sibyl_core.models.entities import EntityType

log = structlog.get_logger()

VALID_ENTITY_TYPES = {t.value for t in EntityType}
AUTO_LINK_ENTITY_TYPES = (
    EntityType.PATTERN,
    EntityType.RULE,
    EntityType.TEMPLATE,
    EntityType.GUIDE,
    EntityType.TOPIC,
)

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


def _project_id_for_policy(entity: Any) -> str | None:
    metadata = getattr(entity, "metadata", {}) or {}
    entity_type = _serialize_enum(
        getattr(entity, "entity_type", None) or metadata.get("entity_type")
    )
    if str(entity_type or "").lower() == EntityType.PROJECT.value:
        entity_id = getattr(entity, "id", None) or metadata.get("id")
        return str(entity_id) if entity_id is not None else None

    project_id = getattr(entity, "project_id", None) or metadata.get("project_id")
    return str(project_id) if project_id is not None else None


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


def _is_surreal_driver(driver: Any) -> bool:
    driver_class = driver.__class__
    return driver_class.__module__.startswith("sibyl_core.backends.surreal")


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


async def get_project_tags(runtime_or_client: Any, project_id: str) -> list[str]:
    """Fetch all unique tags from a project's existing tasks.

    Args:
        runtime_or_client: Legacy graph runtime or graph client instance
        project_id: Project ID to get tags from

    Returns:
        List of unique tags used in the project
    """
    try:
        all_tags: set[str] = set()
        state = getattr(runtime_or_client, "__dict__", {})
        entity_manager = state.get("entity_manager") if isinstance(state, dict) else None
        if entity_manager is not None and callable(getattr(entity_manager, "list_by_type", None)):
            tasks = await entity_manager.list_by_type(
                EntityType.TASK,
                project_id=project_id,
                limit=1000,
                include_archived=True,
            )
            rows = [_get_field(task, "tags", []) for task in tasks]
        else:
            client = (
                state.get("client", runtime_or_client)
                if isinstance(state, dict)
                else runtime_or_client
            )
            driver = getattr(client, "driver", None)
            if driver is not None and _is_surreal_driver(driver):
                group_id = getattr(driver, "group_id", None)
                if not group_id:
                    return []

                result = await driver.execute_query(
                    """
                    SELECT tags, attributes
                    FROM entity
                    WHERE group_id = $group_id
                      AND entity_type = 'task'
                      AND project_id = $project_id
                    LIMIT 1000;
                    """,
                    group_id=group_id,
                    project_id=project_id,
                )
                normalized_rows = client.normalize_result(result)
                rows = []
                for row in normalized_rows:
                    if not isinstance(row, dict):
                        continue
                    rows.append(row.get("tags"))
                    attributes = row.get("attributes")
                    if isinstance(attributes, dict):
                        rows.append(attributes.get("tags"))
                all_tags.update(
                    t.lower()
                    for tags in rows
                    if isinstance(tags, list)
                    for t in tags
                    if isinstance(t, str)
                )
                return sorted(all_tags)

            query = """
                MATCH (n)
                WHERE (n:Episodic OR n:Entity)
                  AND n.entity_type = 'task'
                  AND n.project_id = $project_id
                  AND n.tags IS NOT NULL
                RETURN DISTINCT n.tags as tags
                """

            result = await client.driver.execute_query(
                query,
                project_id=project_id,
            )
            normalized_rows = client.normalize_result(result)
            rows = [
                row[0] if isinstance(row, list | tuple) else row.get("tags")
                for row in normalized_rows
            ]

        for tags in rows:
            if isinstance(tags, list):
                all_tags.update(t.lower() for t in tags if isinstance(t, str))
            elif isinstance(tags, str):
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
    entity_manager: Any,
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

    try:
        if not await _has_auto_link_candidates(entity_manager):
            return []

        results = await entity_manager.search(
            query=query,
            entity_types=AUTO_LINK_ENTITY_TYPES,
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


async def _has_auto_link_candidates(entity_manager: Any) -> bool:
    count_by_type = getattr(entity_manager, "count_by_type", None)
    if count_by_type is None:
        return True
    try:
        counts = await count_by_type(include_archived=False)
    except TypeError:
        try:
            counts = await count_by_type()
        except Exception as e:
            log.debug("auto_discover_candidate_count_failed", error=str(e))
            return True
    except Exception as e:
        log.debug("auto_discover_candidate_count_failed", error=str(e))
        return True
    if not isinstance(counts, Mapping):
        return True
    return any(
        int(counts.get(entity_type.value) or 0) > 0 for entity_type in AUTO_LINK_ENTITY_TYPES
    )
