"""Core authentication primitives.

This module contains JWT and password handling that are used across
both the server and CLI. HTTP-specific auth (middleware, dependencies)
remains in sibyl-server.
"""

from sibyl_core.auth.context import AuthContext
from sibyl_core.auth.contracts import (
    GitHubUserIdentity,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PasswordChange,
    SessionRepository,
    UserRepository,
)
from sibyl_core.auth.jwt import (
    create_access_token,
    decode_token_unverified,
    verify_access_token,
)
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_read,
    authorize_memory_reflect,
    authorize_memory_share,
    authorize_memory_write,
)
from sibyl_core.auth.models import (
    AuthMembership,
    AuthOrganization,
    AuthSession,
    AuthUser,
    OrganizationRole,
    ProjectRole,
    ProjectVisibility,
)
from sibyl_core.auth.passwords import hash_password, verify_password

__all__ = [
    "AuthContext",
    "AuthMembership",
    "AuthOrganization",
    "AuthSession",
    "AuthUser",
    "GitHubUserIdentity",
    "MemoryPolicyAction",
    "MemoryPolicyDecision",
    "OrganizationMembershipRepository",
    "OrganizationRepository",
    "OrganizationRole",
    "PasswordChange",
    "ProjectRole",
    "ProjectVisibility",
    "SessionRepository",
    "UserRepository",
    "authorize_memory_read",
    "authorize_memory_reflect",
    "authorize_memory_share",
    "authorize_memory_write",
    "create_access_token",
    "decode_token_unverified",
    "hash_password",
    "verify_access_token",
    "verify_password",
]
