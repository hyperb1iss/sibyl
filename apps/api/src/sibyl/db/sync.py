"""Project synchronization between graph and Postgres.

Ensures projects in the knowledge graph have corresponding rows in Postgres
for RBAC enforcement. During migration, use sync_projects_from_graph() to
backfill missing projects.
"""

import re
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.db.models import Project, ProjectVisibility

log = structlog.get_logger()


def _slugify(name: str) -> str:
    """Convert a project name to a URL-safe slug."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:64] or "project"


async def sync_projects_from_graph(
    session: AsyncSession,
    organization_id: UUID,
    owner_user_id: UUID,
    graph_projects: list[dict],
    *,
    dry_run: bool = False,
) -> dict:
    """Sync graph projects to Postgres.

    For each graph project not already in Postgres, creates a new Project row
    with default visibility (ORG) and default role (VIEWER).

    Args:
        session: Database session
        organization_id: Organization UUID
        owner_user_id: User UUID to set as owner for new projects
        graph_projects: List of graph project dicts with 'id', 'name', 'description'
        dry_run: If True, don't actually create rows

    Returns:
        Dict with 'created', 'skipped', 'errors' counts and 'details' list
    """
    result = {
        "created": 0,
        "skipped": 0,
        "errors": 0,
        "details": [],
    }

    # Get existing graph_project_ids in Postgres
    existing_result = await session.execute(
        select(Project.graph_project_id).where(Project.organization_id == organization_id)
    )
    existing_ids = {row[0] for row in existing_result.all()}

    for graph_project in graph_projects:
        graph_id = graph_project.get("id") or graph_project.get("uuid")
        name = graph_project.get("name") or graph_project.get("title") or "Untitled"
        description = graph_project.get("description") or ""

        if not graph_id:
            log.warning("skip_project_no_id", project=graph_project)
            result["errors"] += 1
            result["details"].append({"name": name, "error": "No graph ID"})
            continue

        if graph_id in existing_ids:
            log.debug("skip_project_exists", graph_id=graph_id, name=name)
            result["skipped"] += 1
            result["details"].append({"graph_id": graph_id, "name": name, "status": "exists"})
            continue

        # Generate unique slug
        base_slug = _slugify(name)
        slug = base_slug
        suffix = 1

        # Check for slug collisions
        while True:
            slug_check = await session.execute(
                select(Project.id).where(
                    Project.organization_id == organization_id,
                    Project.slug == slug,
                )
            )
            if slug_check.first() is None:
                break
            suffix += 1
            slug = f"{base_slug}-{suffix}"
            if suffix > 100:
                log.error("slug_collision_limit", graph_id=graph_id, name=name)
                result["errors"] += 1
                result["details"].append({"graph_id": graph_id, "name": name, "error": "Slug collision"})
                break

        if suffix > 100:
            continue

        if dry_run:
            log.info("dry_run_would_create", graph_id=graph_id, name=name, slug=slug)
            result["created"] += 1
            result["details"].append({"graph_id": graph_id, "name": name, "slug": slug, "status": "would_create"})
            continue

        # Create new project
        try:
            project = Project(
                organization_id=organization_id,
                owner_user_id=owner_user_id,
                name=name,
                slug=slug,
                description=description[:2000] if description else None,
                graph_project_id=graph_id,
                visibility=ProjectVisibility.ORG,
                # default_role uses model default (VIEWER)
            )
            session.add(project)
            await session.flush()

            log.info("project_synced", graph_id=graph_id, name=name, postgres_id=str(project.id))
            result["created"] += 1
            result["details"].append({
                "graph_id": graph_id,
                "name": name,
                "slug": slug,
                "postgres_id": str(project.id),
                "status": "created",
            })
            existing_ids.add(graph_id)

        except Exception as e:
            log.exception("project_sync_error", graph_id=graph_id, name=name, error=str(e))
            result["errors"] += 1
            result["details"].append({"graph_id": graph_id, "name": name, "error": str(e)})

    return result


async def get_graph_projects(organization_id: str) -> list[dict]:
    """Fetch all projects from the knowledge graph.

    Args:
        organization_id: Organization UUID as string (group_id for graph)

    Returns:
        List of project dicts with id, name, description
    """
    from sibyl_core.graph.client import get_graph_client
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.models.entities import EntityType

    client = await get_graph_client()
    manager = EntityManager(client, group_id=organization_id)

    # List all project entities
    projects = await manager.list_by_type(entity_type=EntityType.PROJECT, limit=1000)

    return [
        {
            "id": p.id,
            "name": getattr(p, "title", None) or getattr(p, "name", "Untitled"),
            "description": getattr(p, "description", "") or "",
        }
        for p in projects
    ]
