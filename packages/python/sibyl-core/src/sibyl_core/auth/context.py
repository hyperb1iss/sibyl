"""AuthContext: resolved auth + tenancy for a request."""

from __future__ import annotations

from dataclasses import dataclass

from sibyl_core.auth.models import (
    AuthOrganization,
    AuthUser,
    OrganizationRole,
    coerce_auth_organization,
    coerce_auth_user,
    coerce_organization_role,
)


@dataclass(frozen=True)
class AuthContext:
    user: AuthUser
    organization: AuthOrganization | None
    org_role: OrganizationRole | None
    scopes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "user", coerce_auth_user(self.user))
        object.__setattr__(self, "organization", coerce_auth_organization(self.organization))
        object.__setattr__(self, "org_role", coerce_organization_role(self.org_role))
        object.__setattr__(self, "scopes", frozenset(str(scope) for scope in self.scopes))

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_org_scoped(self) -> bool:
        return self.organization is not None

    @property
    def user_id(self) -> str | None:
        """Get user ID as string for convenience."""
        return str(self.user.id) if self.user else None

    @property
    def organization_id(self) -> str | None:
        """Get organization ID as string for convenience."""
        return str(self.organization.id) if self.organization else None
