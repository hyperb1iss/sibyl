"""Legacy relational project sync helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from sibyl.db.connection import get_session
from sibyl.db.models import OrganizationMember, Project
from sibyl.db.sync import get_graph_projects, sync_projects_from_graph
from sibyl_core.auth import OrganizationRole


class ProjectSyncResult(TypedDict):
    created: int
    skipped: int
    errors: int
    details: list[dict[str, object]]


@dataclass(frozen=True)
class SharedProjectReference:
    id: UUID
    name: str
    graph_project_id: str


@dataclass(frozen=True)
class LegacyProjectSyncResult:
    graph_projects: list[dict[str, object]]
    owner_user_id: UUID | None
    result: ProjectSyncResult | None


class MissingProjectOwnerError(RuntimeError):
    pass


async def get_shared_project_reference(organization_id: UUID) -> SharedProjectReference | None:
    async with get_session() as session:
        result = await session.execute(
            select(Project).where(
                col(Project.organization_id) == organization_id,
                col(Project.is_shared) == True,  # noqa: E712
            )
        )
        shared_project = result.scalar_one_or_none()

    if shared_project is None:
        return None

    return SharedProjectReference(
        id=shared_project.id,
        name=shared_project.name,
        graph_project_id=shared_project.graph_project_id,
    )


async def sync_graph_projects_to_relational(
    *,
    organization_id: UUID,
    owner_user_id: UUID | None,
    dry_run: bool,
) -> LegacyProjectSyncResult:
    graph_projects = await get_graph_projects(str(organization_id))
    if not graph_projects:
        return LegacyProjectSyncResult(
            graph_projects=[],
            owner_user_id=owner_user_id,
            result=None,
        )

    async with get_session() as session:
        if owner_user_id is None:
            admin_result = await session.execute(
                select(col(OrganizationMember.user_id))
                .where(
                    col(OrganizationMember.organization_id) == organization_id,
                    col(OrganizationMember.role).in_(
                        [OrganizationRole.OWNER, OrganizationRole.ADMIN]
                    ),
                )
                .limit(1)
            )
            row = admin_result.first()
            if row is None:
                raise MissingProjectOwnerError("No org admin found to set as project owner")
            owner_user_id = row[0]

        result = await sync_projects_from_graph(
            session,
            organization_id,
            owner_user_id,
            graph_projects,
            dry_run=dry_run,
        )

        if not dry_run:
            await session.commit()

    return LegacyProjectSyncResult(
        graph_projects=graph_projects,
        owner_user_id=owner_user_id,
        result=result,
    )


__all__ = [
    "LegacyProjectSyncResult",
    "MissingProjectOwnerError",
    "ProjectSyncResult",
    "SharedProjectReference",
    "get_shared_project_reference",
    "sync_graph_projects_to_relational",
]
