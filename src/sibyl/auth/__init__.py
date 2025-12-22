"""Authentication and authorization primitives for Sibyl."""

from sibyl.auth.organizations import OrganizationManager
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.users import GitHubUserIdentity, UserManager
from sibyl.auth.jwt import JwtError, create_access_token, verify_access_token
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, require_org_admin

__all__ = [
    "AuthContext",
    "GitHubUserIdentity",
    "JwtError",
    "OrganizationManager",
    "OrganizationMembershipManager",
    "UserManager",
    "create_access_token",
    "get_auth_context",
    "require_org_admin",
    "verify_access_token",
]
