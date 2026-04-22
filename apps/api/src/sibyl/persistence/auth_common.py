"""Shared auth runtime primitives across persistence backends."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sibyl_core.auth import (
    AuthContext,
    OrganizationMembershipRepository,
    OrganizationRepository,
    UserRepository,
)


class InvalidAuthClaimsError(ValueError):
    """JWT/API-key claims are present but unusable."""


class UserNotFoundError(LookupError):
    """Claims referenced a user that no longer exists."""


class RepositoryAuthContextResolver:
    """Build AuthContext from backend-agnostic auth repositories."""

    def __init__(
        self,
        *,
        users: UserRepository,
        organizations: OrganizationRepository,
        memberships: OrganizationMembershipRepository,
    ) -> None:
        self._users = users
        self._organizations = organizations
        self._memberships = memberships

    async def resolve(self, claims: Mapping[str, Any]) -> AuthContext:
        user_id = self._parse_subject(claims)
        user = await self._users.get_by_id(user_id)
        if user is None:
            msg = f"User not found: {user_id}"
            raise UserNotFoundError(msg)

        organization = None
        membership = None
        raw_org_id = claims.get("org")
        if raw_org_id:
            organization_id = self._parse_optional_uuid(raw_org_id)
            if organization_id is not None:
                organization = await self._organizations.get_by_id(organization_id)
                if organization is not None:
                    membership = await self._memberships.get_for_user(organization.id, user.id)

        scopes = frozenset(str(scope) for scope in claims.get("scopes", []))
        return AuthContext(
            user=user,
            organization=organization,
            org_role=membership.role if membership is not None else None,
            scopes=scopes,
        )

    def _parse_subject(self, claims: Mapping[str, Any]) -> UUID:
        try:
            return UUID(str(claims.get("sub", "")))
        except ValueError as e:
            raise InvalidAuthClaimsError("Invalid token") from e

    def _parse_optional_uuid(self, value: object) -> UUID | None:
        try:
            return UUID(str(value))
        except ValueError:
            return None
