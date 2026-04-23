"""Active organization runtime adapters for the configured auth backend."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from sibyl.config import settings
from sibyl.db.models import ProjectRole

_RUNTIME_EXPORTS = [
    "accept_legacy_org_invitation",
    "add_legacy_org_member",
    "add_legacy_project_member",
    "create_legacy_org",
    "create_legacy_org_invitation",
    "delete_legacy_org",
    "delete_legacy_org_invitation",
    "get_legacy_org",
    "list_legacy_org_invitations",
    "list_legacy_org_members",
    "list_legacy_orgs",
    "list_legacy_project_members",
    "remove_legacy_org_member",
    "remove_legacy_project_member",
    "switch_legacy_org",
    "update_legacy_org",
    "update_legacy_org_member_role",
    "update_legacy_project_member_role",
]

_BACKEND_EXPORTS: dict[str, dict[str, tuple[str, str]]] = {
    "postgres": {
        "accept_legacy_org_invitation": (
            "sibyl.persistence.legacy.org_invitations",
            "accept_legacy_org_invitation",
        ),
        "add_legacy_org_member": ("sibyl.persistence.legacy.org_members", "add_legacy_org_member"),
        "add_legacy_project_member": (
            "sibyl.persistence.legacy.project_members",
            "add_legacy_project_member",
        ),
        "create_legacy_org": ("sibyl.persistence.legacy.orgs", "create_legacy_org"),
        "create_legacy_org_invitation": (
            "sibyl.persistence.legacy.org_invitations",
            "create_legacy_org_invitation",
        ),
        "delete_legacy_org": ("sibyl.persistence.legacy.orgs", "delete_legacy_org"),
        "delete_legacy_org_invitation": (
            "sibyl.persistence.legacy.org_invitations",
            "delete_legacy_org_invitation",
        ),
        "get_legacy_org": ("sibyl.persistence.legacy.orgs", "get_legacy_org"),
        "list_legacy_org_invitations": (
            "sibyl.persistence.legacy.org_invitations",
            "list_legacy_org_invitations",
        ),
        "list_legacy_org_members": (
            "sibyl.persistence.legacy.org_members",
            "list_legacy_org_members",
        ),
        "list_legacy_orgs": ("sibyl.persistence.legacy.orgs", "list_legacy_orgs"),
        "list_legacy_project_members": (
            "sibyl.persistence.legacy.project_members",
            "list_legacy_project_members",
        ),
        "remove_legacy_org_member": (
            "sibyl.persistence.legacy.org_members",
            "remove_legacy_org_member",
        ),
        "remove_legacy_project_member": (
            "sibyl.persistence.legacy.project_members",
            "remove_legacy_project_member",
        ),
        "switch_legacy_org": ("sibyl.persistence.legacy.orgs", "switch_legacy_org"),
        "update_legacy_org": ("sibyl.persistence.legacy.orgs", "update_legacy_org"),
        "update_legacy_org_member_role": (
            "sibyl.persistence.legacy.org_members",
            "update_legacy_org_member_role",
        ),
        "update_legacy_project_member_role": (
            "sibyl.persistence.legacy.project_members",
            "update_legacy_project_member_role",
        ),
    },
    "surreal": {
        name: ("sibyl.persistence.surreal.organization_runtime", name) for name in _RUNTIME_EXPORTS
    },
}

__all__ = list(_RUNTIME_EXPORTS)
__all__.insert(0, "can_manage_legacy_project_members")


def _resolve_backend_export(name: str) -> Any:
    module_path, attr_name = _BACKEND_EXPORTS[settings.auth_store][name]
    return getattr(import_module(module_path), attr_name)


def can_manage_legacy_project_members(
    role: ProjectRole | None,
    project: Any,
    user: Any,
) -> bool:
    """Return whether the actor can manage project members."""
    if project.owner_user_id == user.id:
        return True
    return role in {ProjectRole.OWNER, ProjectRole.MAINTAINER}


def _make_runtime_proxy(name: str) -> Any:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = _resolve_backend_export(name)
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return _proxy


for _export_name in _RUNTIME_EXPORTS:
    if _export_name not in globals():
        globals()[_export_name] = _make_runtime_proxy(_export_name)
