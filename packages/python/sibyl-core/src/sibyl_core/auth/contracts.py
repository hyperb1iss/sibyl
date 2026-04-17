"""Backend-agnostic auth repository contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sibyl_core.auth.models import (
    AuthMembership,
    AuthOrganization,
    AuthSession,
    AuthUser,
    OrganizationRole,
)


class GitHubUserIdentity(BaseModel):
    """Normalized subset of the GitHub user payload."""

    model_config = ConfigDict(populate_by_name=True)

    github_id: int = Field(..., alias="id")
    login: str
    email: str | None = None
    name: str | None = None
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class PasswordChange:
    current_password: str | None
    new_password: str


@runtime_checkable
class UserRepository(Protocol):
    async def get_by_id(self, user_id: UUID) -> AuthUser | None: ...

    async def has_any_users(self) -> bool: ...

    async def get_by_github_id(self, github_id: int) -> AuthUser | None: ...

    async def get_by_email(self, email: str) -> AuthUser | None: ...

    async def upsert_from_github(
        self, identity: GitHubUserIdentity, *, is_admin: bool = False
    ) -> AuthUser: ...

    async def create_local_user(
        self, *, email: str, password: str, name: str, is_admin: bool = False
    ) -> AuthUser: ...

    async def authenticate_local(self, *, email: str, password: str) -> AuthUser | None: ...

    async def update_profile(
        self,
        user: AuthUser,
        *,
        email: str | None = None,
        name: str | None = None,
        avatar_url: str | None = None,
    ) -> AuthUser: ...

    async def change_password(self, user: AuthUser, change: PasswordChange) -> AuthUser: ...


@runtime_checkable
class OrganizationRepository(Protocol):
    async def get_by_id(self, org_id: UUID) -> AuthOrganization | None: ...

    async def get_by_slug(self, slug: str) -> AuthOrganization | None: ...

    async def list_all(self, limit: int = 100) -> list[AuthOrganization]: ...

    async def create(
        self,
        *,
        name: str,
        slug: str | None = None,
        is_personal: bool = False,
        settings: dict[str, object] | None = None,
    ) -> AuthOrganization: ...

    async def update(
        self,
        organization: AuthOrganization,
        *,
        name: str | None = None,
        slug: str | None = None,
        settings: dict[str, object] | None = None,
    ) -> AuthOrganization: ...

    async def delete(self, organization: AuthOrganization) -> None: ...

    async def create_personal_for_user(self, user: AuthUser) -> AuthOrganization: ...


@runtime_checkable
class OrganizationMembershipRepository(Protocol):
    async def get(self, membership_id: UUID) -> AuthMembership | None: ...

    async def get_for_user(self, organization_id: UUID, user_id: UUID) -> AuthMembership | None: ...

    async def list_for_org(self, organization_id: UUID) -> list[AuthMembership]: ...

    async def add_member(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        role: OrganizationRole = OrganizationRole.MEMBER,
    ) -> AuthMembership: ...

    async def remove_member(self, *, organization_id: UUID, user_id: UUID) -> None: ...

    async def set_role(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        role: OrganizationRole,
    ) -> AuthMembership: ...


@runtime_checkable
class SessionRepository(Protocol):
    @staticmethod
    def hash_token(token: str) -> str: ...

    async def create_session(
        self,
        *,
        user_id: UUID,
        token: str,
        expires_at: datetime,
        organization_id: UUID | None = None,
        refresh_token: str | None = None,
        refresh_token_expires_at: datetime | None = None,
        device_name: str | None = None,
        device_type: str | None = None,
        browser: str | None = None,
        os: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        location: str | None = None,
    ) -> AuthSession: ...

    async def get_session_by_token(self, token: str) -> AuthSession | None: ...

    async def get_session_by_refresh_token(self, refresh_token: str) -> AuthSession | None: ...

    async def rotate_tokens(
        self,
        session: AuthSession,
        *,
        new_access_token: str,
        new_access_expires_at: datetime,
        new_refresh_token: str,
        new_refresh_expires_at: datetime,
    ) -> AuthSession: ...

    async def list_user_sessions(
        self, user_id: UUID, *, include_expired: bool = False
    ) -> list[AuthSession]: ...

    async def update_activity(self, token: str) -> bool: ...

    async def mark_current(self, token: str) -> bool: ...

    async def revoke_session(self, session_id: UUID, user_id: UUID) -> bool: ...

    async def revoke_all_sessions(
        self, user_id: UUID, *, exclude_token_hash: str | None = None
    ) -> int: ...

    async def cleanup_expired(self, *, older_than_days: int = 30) -> int: ...
