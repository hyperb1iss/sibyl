"""Surreal-backed persistence adapters."""

from sibyl.persistence.surreal.auth import (
    SurrealAuthContextResolver,
    SurrealOrganizationMembershipRepository,
    SurrealOrganizationRepository,
    SurrealUserRepository,
    build_surreal_auth_client,
)
from sibyl.persistence.surreal.content import (
    build_surreal_content_client,
    close_shared_surreal_content_client,
    get_shared_surreal_content_client,
)

__all__ = [
    "SurrealAuthContextResolver",
    "SurrealOrganizationMembershipRepository",
    "SurrealOrganizationRepository",
    "SurrealUserRepository",
    "build_surreal_auth_client",
    "build_surreal_content_client",
    "close_shared_surreal_content_client",
    "get_shared_surreal_content_client",
]
