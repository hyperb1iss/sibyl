"""AuthContext: resolved auth + tenancy for a request."""

from __future__ import annotations

from dataclasses import dataclass

from sibyl.db.models import Organization, OrganizationRole, User


@dataclass(frozen=True)
class AuthContext:
    user: User
    organization: Organization | None
    org_role: OrganizationRole | None
    scopes: frozenset[str] = frozenset()

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
