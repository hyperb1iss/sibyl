"""Project-wide settings endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from sibyl.auth.dependencies import get_current_org_role, get_current_organization, get_current_user
from sibyl.persistence import organization_runtime
from sibyl.persistence.organization_common import ProjectMemorySettings
from sibyl_core.auth import AuthOrganization, AuthUser, OrganizationRole
from sibyl_core.models.memory_scope import MemoryScope

router = APIRouter(prefix="/projects/{project_id}/settings", tags=["project-settings"])

ProjectDefaultMemoryScope = Literal["private", "project"]


class ProjectSettingsResponse(BaseModel):
    project_id: str
    default_memory_scope: ProjectDefaultMemoryScope = Field(
        description=(
            "Where a capture in this project lands when the writer names no scope. "
            "Project scope applies only to writers with contributor access."
        )
    )
    can_manage: bool


class ProjectSettingsUpdateRequest(BaseModel):
    default_memory_scope: ProjectDefaultMemoryScope


def _settings_response(settings: ProjectMemorySettings) -> ProjectSettingsResponse:
    scope = settings.default_memory_scope.value
    return ProjectSettingsResponse(
        project_id=settings.graph_project_id,
        default_memory_scope="project" if scope == MemoryScope.PROJECT.value else "private",
        can_manage=settings.can_manage,
    )


@router.get("", response_model=ProjectSettingsResponse)
async def get_project_settings(
    project_id: str,
    user: AuthUser = Depends(get_current_user),
    org: AuthOrganization = Depends(get_current_organization),
    org_role: OrganizationRole = Depends(get_current_org_role),
) -> ProjectSettingsResponse:
    settings = await organization_runtime.get_project_memory_settings(
        project_id=project_id,
        actor=user,
        org_id=org.id,
        org_role=org_role,
    )
    return _settings_response(settings)


@router.patch("", response_model=ProjectSettingsResponse)
async def update_project_settings(
    request: Request,
    project_id: str,
    body: ProjectSettingsUpdateRequest,
    user: AuthUser = Depends(get_current_user),
    org: AuthOrganization = Depends(get_current_organization),
    org_role: OrganizationRole = Depends(get_current_org_role),
) -> ProjectSettingsResponse:
    settings = await organization_runtime.update_project_memory_settings(
        request=request,
        project_id=project_id,
        actor=user,
        org_id=org.id,
        org_role=org_role,
        default_memory_scope=MemoryScope(body.default_memory_scope),
    )
    return _settings_response(settings)
