"""AuthContext: resolved auth + tenancy for a request."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sibyl_core.auth.models import (
    AuthOrganization,
    AuthUser,
    OrganizationRole,
    coerce_auth_organization,
    coerce_auth_user,
    coerce_organization_role,
)


def _frozen_string_set(values: Iterable[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(str(value) for value in values)


def _optional_string(value: object | None) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class MemoryPolicyContext:
    actor_user_id: str | None
    organization_id: str | None = None
    organization_role: OrganizationRole | str | None = None
    is_global_admin: bool = False
    accessible_projects: frozenset[str] | None = None
    accessible_teams: frozenset[str] | None = None
    accessible_delegations: frozenset[str] | None = None
    delegated_authority: str | None = None
    agent_id: str | None = None
    project_id: str | None = None
    memory_space: str | None = None
    scope_key: str | None = None
    source_surface: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_user_id", _optional_string(self.actor_user_id))
        object.__setattr__(self, "organization_id", _optional_string(self.organization_id))
        object.__setattr__(
            self,
            "organization_role",
            coerce_organization_role(self.organization_role),
        )
        object.__setattr__(self, "is_global_admin", bool(self.is_global_admin))
        object.__setattr__(
            self,
            "accessible_projects",
            _frozen_string_set(self.accessible_projects),
        )
        object.__setattr__(
            self,
            "accessible_teams",
            _frozen_string_set(self.accessible_teams),
        )
        object.__setattr__(
            self,
            "accessible_delegations",
            _frozen_string_set(self.accessible_delegations),
        )
        object.__setattr__(
            self,
            "delegated_authority",
            _optional_string(self.delegated_authority),
        )
        object.__setattr__(self, "agent_id", _optional_string(self.agent_id))
        object.__setattr__(self, "project_id", _optional_string(self.project_id))
        object.__setattr__(self, "memory_space", _optional_string(self.memory_space))
        object.__setattr__(self, "scope_key", _optional_string(self.scope_key))
        object.__setattr__(self, "source_surface", str(self.source_surface or "unknown"))


@dataclass(frozen=True)
class AuthContext:
    user: AuthUser
    organization: AuthOrganization | None
    org_role: OrganizationRole | None
    scopes: frozenset[str] = frozenset()
    api_key_id: str | None = None
    api_key_project_ids: frozenset[str] | None = None
    api_key_memory_space_ids: frozenset[str] | None = None
    api_key_memory_scope_keys: frozenset[str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user", coerce_auth_user(self.user))
        object.__setattr__(self, "organization", coerce_auth_organization(self.organization))
        object.__setattr__(self, "org_role", coerce_organization_role(self.org_role))
        object.__setattr__(self, "scopes", frozenset(str(scope) for scope in self.scopes))
        object.__setattr__(self, "api_key_id", _optional_string(self.api_key_id))
        object.__setattr__(
            self,
            "api_key_project_ids",
            _frozen_string_set(self.api_key_project_ids),
        )
        object.__setattr__(
            self,
            "api_key_memory_space_ids",
            _frozen_string_set(self.api_key_memory_space_ids),
        )
        object.__setattr__(
            self,
            "api_key_memory_scope_keys",
            _frozen_string_set(self.api_key_memory_scope_keys),
        )

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

    def to_memory_policy_context(
        self,
        *,
        memory_space: str | None = None,
        scope_key: str | None = None,
        project_id: str | None = None,
        accessible_projects: Iterable[str] | None = None,
        accessible_teams: Iterable[str] | None = None,
        accessible_delegations: Iterable[str] | None = None,
        delegated_authority: str | None = None,
        agent_id: str | None = None,
        source_surface: str = "rest",
    ) -> MemoryPolicyContext:
        return MemoryPolicyContext(
            actor_user_id=self.user_id,
            organization_id=self.organization_id,
            organization_role=self.org_role,
            is_global_admin=self.user.is_admin,
            accessible_projects=_frozen_string_set(accessible_projects),
            accessible_teams=_frozen_string_set(accessible_teams),
            accessible_delegations=_frozen_string_set(accessible_delegations),
            delegated_authority=delegated_authority,
            agent_id=agent_id,
            project_id=project_id,
            memory_space=memory_space,
            scope_key=scope_key,
            source_surface=source_surface,
        )
