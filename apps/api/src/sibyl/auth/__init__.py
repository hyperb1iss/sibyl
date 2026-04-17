"""Authentication and authorization primitives for Sibyl."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sibyl.auth.context import AuthContext
from sibyl.auth.errors import (
    AuthErrorCode,
    AuthorizationError,
    NoOrgContextError,
    OrgAccessDeniedError,
    OwnershipRequiredError,
    ProjectAccessDeniedError,
    ResourceAccessDeniedError,
)
from sibyl.auth.jwt import JwtError, create_access_token, verify_access_token

if TYPE_CHECKING:
    from sibyl.auth.dependencies import get_auth_context, require_org_admin
    from sibyl.auth.memberships import OrganizationMembershipManager
    from sibyl.auth.organizations import OrganizationManager
    from sibyl.auth.users import GitHubUserIdentity, UserManager

__all__ = [
    # Context
    "AuthContext",
    "get_auth_context",
    # Errors
    "AuthErrorCode",
    "AuthorizationError",
    "NoOrgContextError",
    "OrgAccessDeniedError",
    "OwnershipRequiredError",
    "ProjectAccessDeniedError",
    "ResourceAccessDeniedError",
    # JWT
    "JwtError",
    "create_access_token",
    "verify_access_token",
    # Managers
    "GitHubUserIdentity",
    "OrganizationManager",
    "OrganizationMembershipManager",
    "UserManager",
    # Dependencies
    "require_org_admin",
]


def __getattr__(name: str) -> Any:
    if name in {"get_auth_context", "require_org_admin"}:
        from sibyl.auth.dependencies import get_auth_context, require_org_admin

        exports = {
            "get_auth_context": get_auth_context,
            "require_org_admin": require_org_admin,
        }
        return exports[name]
    if name == "OrganizationMembershipManager":
        from sibyl.auth.memberships import OrganizationMembershipManager

        return OrganizationMembershipManager
    if name == "OrganizationManager":
        from sibyl.auth.organizations import OrganizationManager

        return OrganizationManager
    if name in {"GitHubUserIdentity", "UserManager"}:
        from sibyl.auth.users import GitHubUserIdentity, UserManager

        exports = {
            "GitHubUserIdentity": GitHubUserIdentity,
            "UserManager": UserManager,
        }
        return exports[name]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
