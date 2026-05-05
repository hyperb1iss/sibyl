"""Active organization runtime adapters for the configured auth backend."""

from __future__ import annotations

from collections.abc import Awaitable
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, cast

from sibyl.config import settings
from sibyl.persistence.organization_common import (
    can_manage_project_members as _can_manage_project_members,
)


class RuntimeExport(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> Awaitable[object]: ...


if TYPE_CHECKING:
    from uuid import UUID

    from starlette.requests import Request

    from sibyl.persistence.organization_common import (
        InvitationAcceptance,
        InvitationRecord,
        OrgAuthResult,
        OrgMemberChange,
        OrgRoleResult,
        OrgSummary,
        ProjectMemberChange,
        ProjectMembersResult,
    )
    from sibyl_core.auth import OrganizationRole, ProjectRole

    class ActorWithId(Protocol):
        id: UUID

    class InvitationUser(Protocol):
        id: UUID
        email: str | None

    class AcceptOrgInvitation(Protocol):
        def __call__(
            self, *, token: str, user: InvitationUser, request: Request
        ) -> Awaitable[InvitationAcceptance]: ...

    class AddOrgMember(Protocol):
        def __call__(
            self,
            *,
            slug: str,
            actor_id: UUID,
            target_user_id: UUID,
            role: OrganizationRole,
            request: Request,
        ) -> Awaitable[OrgMemberChange]: ...

    class AddProjectMember(Protocol):
        def __call__(
            self,
            *,
            request: Request,
            project_id: str,
            actor: ActorWithId,
            org_id: UUID,
            target_user_id: UUID,
            role: ProjectRole,
        ) -> Awaitable[ProjectMemberChange]: ...

    class CreateOrg(Protocol):
        def __call__(
            self,
            *,
            request: Request,
            user_id: UUID,
            name: str,
            slug: str | None = None,
        ) -> Awaitable[OrgAuthResult]: ...

    class CreateOrgInvitation(Protocol):
        def __call__(
            self,
            *,
            slug: str,
            actor_id: UUID,
            email: str,
            role: OrganizationRole,
            expires_days: int,
            request: Request,
        ) -> Awaitable[InvitationRecord]: ...

    class DeleteOrg(Protocol):
        def __call__(self, *, request: Request, slug: str, user_id: UUID) -> Awaitable[None]: ...

    class DeleteOrgInvitation(Protocol):
        def __call__(
            self, *, slug: str, actor_id: UUID, invitation_id: UUID, request: Request
        ) -> Awaitable[None]: ...

    class GetOrg(Protocol):
        def __call__(self, *, slug: str, user_id: UUID) -> Awaitable[OrgRoleResult]: ...

    class ListOrgIds(Protocol):
        def __call__(self) -> Awaitable[list[str]]: ...

    class ListOrgInvitations(Protocol):
        def __call__(self, *, slug: str, actor_id: UUID) -> Awaitable[list[InvitationRecord]]: ...

    class ListOrgMembers(Protocol):
        def __call__(self, *, slug: str, actor_id: UUID) -> Awaitable[list[dict[str, object]]]: ...

    class ListOrgs(Protocol):
        def __call__(self, *, user_id: UUID) -> Awaitable[list[OrgSummary]]: ...

    class ListProjectMembers(Protocol):
        def __call__(
            self, *, project_id: str, actor: ActorWithId, org_id: UUID
        ) -> Awaitable[ProjectMembersResult]: ...

    class RemoveOrgMember(Protocol):
        def __call__(
            self,
            *,
            slug: str,
            actor_id: UUID,
            target_user_id: UUID,
            request: Request,
        ) -> Awaitable[OrgMemberChange]: ...

    class RemoveProjectMember(Protocol):
        def __call__(
            self,
            *,
            request: Request,
            project_id: str,
            actor: ActorWithId,
            org_id: UUID,
            target_user_id: UUID,
        ) -> Awaitable[ProjectMemberChange]: ...

    class SwitchOrg(Protocol):
        def __call__(
            self, *, request: Request, slug: str, user_id: UUID
        ) -> Awaitable[OrgAuthResult]: ...

    class UpdateOrg(Protocol):
        def __call__(
            self,
            *,
            request: Request,
            slug: str,
            user_id: UUID,
            name: str | None = None,
            new_slug: str | None = None,
        ) -> Awaitable[OrgSummary]: ...

    class UpdateOrgMemberRole(Protocol):
        def __call__(
            self,
            *,
            slug: str,
            actor_id: UUID,
            target_user_id: UUID,
            role: OrganizationRole,
            request: Request,
        ) -> Awaitable[OrgMemberChange]: ...

    class UpdateProjectMemberRole(Protocol):
        def __call__(
            self,
            *,
            request: Request,
            project_id: str,
            actor: ActorWithId,
            org_id: UUID,
            target_user_id: UUID,
            role: ProjectRole,
        ) -> Awaitable[ProjectMemberChange]: ...

    accept_org_invitation: AcceptOrgInvitation
    add_org_member: AddOrgMember
    add_project_member: AddProjectMember
    create_org: CreateOrg
    create_org_invitation: CreateOrgInvitation
    delete_org: DeleteOrg
    delete_org_invitation: DeleteOrgInvitation
    get_org: GetOrg
    list_org_ids: ListOrgIds
    list_org_invitations: ListOrgInvitations
    list_org_members: ListOrgMembers
    list_orgs: ListOrgs
    list_project_members: ListProjectMembers
    remove_org_member: RemoveOrgMember
    remove_project_member: RemoveProjectMember
    switch_org: SwitchOrg
    update_org: UpdateOrg
    update_org_member_role: UpdateOrgMemberRole
    update_project_member_role: UpdateProjectMemberRole

_BACKEND_EXPORTS = [
    "accept_org_invitation",
    "add_org_member",
    "add_project_member",
    "create_org",
    "create_org_invitation",
    "delete_org",
    "delete_org_invitation",
    "get_org",
    "list_org_ids",
    "list_org_invitations",
    "list_org_members",
    "list_orgs",
    "list_project_members",
    "remove_org_member",
    "remove_project_member",
    "switch_org",
    "update_org",
    "update_org_member_role",
    "update_project_member_role",
]

_BACKEND_MODULES = {
    "postgres": (
        "sibyl.persistence.legacy.orgs",
        "sibyl.persistence.legacy.org_members",
        "sibyl.persistence.legacy.org_invitations",
        "sibyl.persistence.legacy.project_members",
    ),
    "surreal": ("sibyl.persistence.surreal.organization_runtime",),
}

__all__ = [
    "can_manage_project_members",
    "accept_org_invitation",
    "add_org_member",
    "add_project_member",
    "create_org",
    "create_org_invitation",
    "delete_org",
    "delete_org_invitation",
    "get_org",
    "list_org_ids",
    "list_org_invitations",
    "list_org_members",
    "list_orgs",
    "list_project_members",
    "remove_org_member",
    "remove_project_member",
    "switch_org",
    "update_org",
    "update_org_member_role",
    "update_project_member_role",
]

can_manage_project_members = _can_manage_project_members


def _resolve_backend_export(name: str) -> RuntimeExport:
    for module_path in _BACKEND_MODULES[settings.auth_store]:
        module = import_module(module_path)
        if hasattr(module, name):
            return cast("RuntimeExport", getattr(module, name))
    msg = f"{name} is not implemented for SIBYL_AUTH_STORE={settings.auth_store!r}"
    raise AttributeError(msg)


def _make_runtime_proxy(name: str) -> RuntimeExport:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = _resolve_backend_export(name)
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return cast("RuntimeExport", _proxy)


for _export_name in _BACKEND_EXPORTS:
    if _export_name not in globals():
        globals()[_export_name] = _make_runtime_proxy(_export_name)


del _export_name
