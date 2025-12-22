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

