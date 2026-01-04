"""Project sync: Graph → Postgres for RBAC enforcement.

Graph is source of truth for project content. Postgres mirrors for RBAC:
- Create: When graph project created, create Postgres row
- Update: When graph project updated, update Postgres row (name, description)
- Delete: When graph project deleted, delete Postgres row (cascades members)

This enables project-level RBAC without changing the graph-first architecture.
"""

import re
from uuid import UUID

import structlog
from sqlalchemy import delete, select, update
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


async def _generate_unique_slug(
    session: AsyncSession, organization_id: UUID, name: str, exclude_id: UUID | None = None
) -> str:
    """Generate a unique slug for a project within an organization."""
    base_slug = _slugify(name)
    slug = base_slug
    suffix = 1

    while suffix <= 100:
        query = select(Project.id).where(
            Project.organization_id == organization_id,
            Project.slug == slug,
        )
        if exclude_id:
            query = query.where(Project.id != exclude_id)

        result = await session.execute(query)
        if result.first() is None:
            return slug

        suffix += 1
        slug = f"{base_slug}-{suffix}"

    # Fallback: append random suffix
    import secrets

    return f"{base_slug}-{secrets.token_hex(4)}"


async def sync_project_create(
    session: AsyncSession,
    *,
    organization_id: UUID,
    owner_user_id: UUID,
    graph_project_id: str,
    name: str,
    description: str | None = None,
) -> Project:
    """Create Postgres project row when graph project is created.

    Args:
        session: Database session
        organization_id: Organization UUID
        owner_user_id: User who created the project (becomes owner)
        graph_project_id: Graph entity ID (e.g. project_abc123)
        name: Project display name
        description: Optional project description

    Returns:
        Created Project model
    """
    # Check if already exists (idempotent)
    existing = await session.execute(
        select(Project).where(
            Project.organization_id == organization_id,
            Project.graph_project_id == graph_project_id,
        )
    )
    if project := existing.scalar_one_or_none():
        log.debug("project_already_synced", graph_id=graph_project_id)
        return project

    slug = await _generate_unique_slug(session, organization_id, name)

    project = Project(
        organization_id=organization_id,
        owner_user_id=owner_user_id,
        name=name,
        slug=slug,
        description=description[:2000] if description else None,
        graph_project_id=graph_project_id,
        visibility=ProjectVisibility.ORG,
        # default_role uses model default (VIEWER)
    )
    session.add(project)
    await session.flush()

    log.info(
        "project_synced_create",
        graph_id=graph_project_id,
        postgres_id=str(project.id),
        name=name,
    )
    return project


async def sync_project_update(
    session: AsyncSession,
    *,
    organization_id: UUID,
    graph_project_id: str,
    name: str | None = None,
    description: str | None = None,
) -> bool:
    """Update Postgres project row when graph project is updated.

    Only syncs name and description (slug regenerated if name changes).

    Args:
        session: Database session
        organization_id: Organization UUID
        graph_project_id: Graph entity ID
        name: New name (if changed)
        description: New description (if changed)

    Returns:
        True if project was found and updated
    """
    # Find existing project
    result = await session.execute(
        select(Project).where(
            Project.organization_id == organization_id,
            Project.graph_project_id == graph_project_id,
        )
    )
    project = result.scalar_one_or_none()

    if not project:
        log.warning("project_not_found_for_update", graph_id=graph_project_id)
        return False

    updates: dict = {}
    if name is not None and name != project.name:
        updates["name"] = name
        # Regenerate slug if name changed
        updates["slug"] = await _generate_unique_slug(
            session, organization_id, name, exclude_id=project.id
        )

    if description is not None:
        updates["description"] = description[:2000] if description else None

    if updates:
        await session.execute(
            update(Project).where(Project.id == project.id).values(**updates)
        )
        log.info("project_synced_update", graph_id=graph_project_id, updates=list(updates.keys()))

    return True


async def sync_project_delete(
    session: AsyncSession,
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> bool:
    """Delete Postgres project row when graph project is deleted.

    This cascades to project_members via FK constraint.

    Args:
        session: Database session
        organization_id: Organization UUID
        graph_project_id: Graph entity ID

    Returns:
        True if project was found and deleted
    """
    result = await session.execute(
        delete(Project).where(
            Project.organization_id == organization_id,
            Project.graph_project_id == graph_project_id,
        )
    )

    if result.rowcount > 0:  # type: ignore[union-attr]
        log.info("project_synced_delete", graph_id=graph_project_id)
        return True

    log.debug("project_not_found_for_delete", graph_id=graph_project_id)
    return False


async def get_postgres_project_by_graph_id(
    session: AsyncSession,
    organization_id: UUID,
    graph_project_id: str,
) -> Project | None:
    """Look up Postgres project by graph ID.

    Useful for resolving graph IDs to Postgres UUIDs for RBAC operations.
    """
    result = await session.execute(
        select(Project).where(
            Project.organization_id == organization_id,
            Project.graph_project_id == graph_project_id,
        )
    )
    return result.scalar_one_or_none()
