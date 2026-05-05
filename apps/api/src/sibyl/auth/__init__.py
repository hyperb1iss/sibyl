"""Authentication and authorization primitives for Sibyl."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
from sibyl_core.auth import GitHubUserIdentity

if TYPE_CHECKING:
    from sibyl.auth.dependencies import get_auth_context, require_org_admin

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
    # Shared auth contracts
    "GitHubUserIdentity",
    # Dependencies
    "require_org_admin",
]


def __getattr__(name: str) -> object:
    if name in {"get_auth_context", "require_org_admin"}:
        from sibyl.auth.dependencies import get_auth_context, require_org_admin

        exports = {
            "get_auth_context": get_auth_context,
            "require_org_admin": require_org_admin,
        }
        return exports[name]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
