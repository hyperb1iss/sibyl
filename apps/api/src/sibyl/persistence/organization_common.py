"""Shared organization runtime DTOs and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sibyl_core.auth import OrganizationRole, ProjectRole


@dataclass
class OrgSummary:
    id: UUID
    slug: str
    name: str
    is_personal: bool = False
    role: OrganizationRole | None = None


@dataclass
class OrgAuthResult:
    id: UUID
    slug: str
    name: str
    access_token: str
    refresh_token: str
    refresh_expires: datetime


@dataclass
class OrgRoleResult:
    id: UUID
    slug: str
    name: str
    role: OrganizationRole


@dataclass
class OrgMemberChange:
    org_id: UUID
    user_id: UUID
    role: OrganizationRole | None = None


@dataclass
class InvitationRecord:
    id: UUID
    email: str
    role: OrganizationRole
    created_at: datetime | None = None
    expires_at: datetime | None = None
    accept_url: str | None = None


@dataclass
class InvitationAcceptance:
    access_token: str
    refresh_token: str
    refresh_expires: datetime
    organization_id: UUID
    invitation_id: UUID
    organization_slug: str | None = None
    organization_name: str | None = None


@dataclass
class ProjectMembersResult:
    members: list[dict[str, object]]
    can_manage: bool


@dataclass
class ProjectMemberChange:
    org_id: UUID
    project_db_id: UUID
    user_id: UUID
    role: ProjectRole | None = None


__all__ = [
    "InvitationAcceptance",
    "InvitationRecord",
    "OrgAuthResult",
    "OrgMemberChange",
    "OrgRoleResult",
    "OrgSummary",
    "ProjectMemberChange",
    "ProjectMembersResult",
    "can_manage_project_members",
]


def can_manage_project_members(
    role: ProjectRole | None,
    project: object,
    user: object,
) -> bool:
    """Return whether the actor can manage project members."""
    owner_user_id = getattr(project, "owner_user_id", None)
    user_id = getattr(user, "id", None)
    if user_id is not None and owner_user_id == user_id:
        return True
    return role in {ProjectRole.OWNER, ProjectRole.MAINTAINER}
