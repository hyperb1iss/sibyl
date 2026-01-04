"""Authentication and authorization primitives for Sibyl."""

from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, require_org_admin
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
