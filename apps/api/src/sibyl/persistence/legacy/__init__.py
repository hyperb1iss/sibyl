"""Legacy persistence adapters for the Postgres and FalkorDB runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sibyl.persistence.legacy.auth import (
        InvalidAuthClaimsError,
        LegacyAuthContextResolver,
        LegacyOrganizationMembershipRepository,
        LegacyOrganizationRepository,
        LegacySessionRepository,
        LegacyUserRepository,
        UserNotFoundError,
    )

__all__ = [
    "InvalidAuthClaimsError",
    "LegacyAuthContextResolver",
    "LegacyOrganizationMembershipRepository",
    "LegacyOrganizationRepository",
    "LegacySessionRepository",
    "LegacyUserRepository",
    "UserNotFoundError",
]


def __getattr__(name: str) -> Any:
    if name in set(__all__):
        from sibyl.persistence.legacy.auth import (
            InvalidAuthClaimsError,
            LegacyAuthContextResolver,
            LegacyOrganizationMembershipRepository,
            LegacyOrganizationRepository,
            LegacySessionRepository,
            LegacyUserRepository,
            UserNotFoundError,
        )

        exports = {
            "InvalidAuthClaimsError": InvalidAuthClaimsError,
            "LegacyAuthContextResolver": LegacyAuthContextResolver,
            "LegacyOrganizationMembershipRepository": LegacyOrganizationMembershipRepository,
            "LegacyOrganizationRepository": LegacyOrganizationRepository,
            "LegacySessionRepository": LegacySessionRepository,
            "LegacyUserRepository": LegacyUserRepository,
            "UserNotFoundError": UserNotFoundError,
        }
        return exports[name]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
