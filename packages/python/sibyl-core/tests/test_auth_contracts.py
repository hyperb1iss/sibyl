from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sibyl_core.auth import (
    AuthContext,
    AuthMembership,
    AuthOrganization,
    AuthSession,
    AuthUser,
    GitHubUserIdentity,
    OrganizationMembershipRepository,
    OrganizationRepository,
    OrganizationRole,
    PasswordChange,
    SessionRepository,
    UserRepository,
)


class FakeUserRepository:
    def __init__(self, user: AuthUser) -> None:
        self.user = user

    async def get_by_id(self, user_id):
        return self.user if self.user.id == user_id else None

    async def has_any_users(self) -> bool:
        return True

    async def get_by_github_id(self, github_id: int) -> AuthUser | None:
        return self.user if self.user.github_id == github_id else None

    async def get_by_email(self, email: str) -> AuthUser | None:
        return self.user if self.user.email == email else None

    async def upsert_from_github(
        self, identity: GitHubUserIdentity, *, is_admin: bool = False
    ) -> AuthUser:
        self.user.github_id = identity.github_id
        self.user.email = identity.email
        self.user.name = identity.name or identity.login
        self.user.avatar_url = identity.avatar_url
        self.user.is_admin = is_admin
        return self.user

    async def create_local_user(
        self, *, email: str, password: str, name: str, is_admin: bool = False
    ) -> AuthUser:
        del password
        return AuthUser(
            id=uuid4(),
            email=email,
            name=name,
            is_admin=is_admin,
        )

    async def authenticate_local(self, *, email: str, password: str) -> AuthUser | None:
        del password
        return self.user if self.user.email == email else None

    async def update_profile(
        self,
        user: AuthUser,
        *,
        email: str | None = None,
        name: str | None = None,
        avatar_url: str | None = None,
    ) -> AuthUser:
        if email is not None:
            user.email = email
        if name is not None:
            user.name = name
        if avatar_url is not None:
            user.avatar_url = avatar_url
        return user

    async def change_password(self, user: AuthUser, change: PasswordChange) -> AuthUser:
        del change
        return user


class FakeOrganizationRepository:
    def __init__(self, organization: AuthOrganization) -> None:
        self.organization = organization

    async def get_by_id(self, org_id):
        return self.organization if self.organization.id == org_id else None

    async def get_by_slug(self, slug: str) -> AuthOrganization | None:
        return self.organization if self.organization.slug == slug else None

    async def list_all(self, limit: int = 100) -> list[AuthOrganization]:
        del limit
        return [self.organization]

    async def create(
        self,
        *,
        name: str,
        slug: str | None = None,
        is_personal: bool = False,
        settings: dict[str, object] | None = None,
    ) -> AuthOrganization:
        return AuthOrganization(
            id=uuid4(),
            name=name,
            slug=slug or name.lower(),
            is_personal=is_personal,
            settings=settings or {},
        )

    async def update(
        self,
        organization: AuthOrganization,
        *,
        name: str | None = None,
        slug: str | None = None,
        settings: dict[str, object] | None = None,
    ) -> AuthOrganization:
        if name is not None:
            organization.name = name
        if slug is not None:
            organization.slug = slug
        if settings is not None:
            organization.settings = settings
        return organization

    async def delete(self, organization: AuthOrganization) -> None:
        del organization

    async def create_personal_for_user(self, user: AuthUser) -> AuthOrganization:
        return AuthOrganization(
            id=uuid4(),
            name=user.name,
            slug=f"u-{user.id.hex}",
            is_personal=True,
        )


class FakeOrganizationMembershipRepository:
    def __init__(self, membership: AuthMembership) -> None:
        self.membership = membership

    async def get(self, membership_id):
        return self.membership if self.membership.id == membership_id else None

    async def get_for_user(self, organization_id, user_id):
        if (
            self.membership.organization_id == organization_id
            and self.membership.user_id == user_id
        ):
            return self.membership
        return None

    async def list_for_org(self, organization_id):
        return [self.membership] if self.membership.organization_id == organization_id else []

    async def add_member(
        self,
        *,
        organization_id,
        user_id,
        role: OrganizationRole = OrganizationRole.MEMBER,
    ):
        self.membership.organization_id = organization_id
        self.membership.user_id = user_id
        self.membership.role = role
        return self.membership

    async def remove_member(self, *, organization_id, user_id) -> None:
        del organization_id, user_id

    async def set_role(self, *, organization_id, user_id, role: OrganizationRole):
        if (
            self.membership.organization_id == organization_id
            and self.membership.user_id == user_id
        ):
            self.membership.role = role
        return self.membership


class FakeSessionRepository:
    def __init__(self, session: AuthSession) -> None:
        self.session = session

    @staticmethod
    def hash_token(token: str) -> str:
        return f"hashed:{token}"

    async def create_session(
        self,
        *,
        user_id,
        token: str,
        expires_at: datetime,
        organization_id=None,
        refresh_token: str | None = None,
        refresh_token_expires_at: datetime | None = None,
        device_name: str | None = None,
        device_type: str | None = None,
        browser: str | None = None,
        os: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        location: str | None = None,
    ):
        del token, refresh_token
        self.session.user_id = user_id
        self.session.organization_id = organization_id
        self.session.expires_at = expires_at
        self.session.refresh_token_expires_at = refresh_token_expires_at
        self.session.device_name = device_name
        self.session.device_type = device_type
        self.session.browser = browser
        self.session.os = os
        self.session.ip_address = ip_address
        self.session.user_agent = user_agent
        self.session.location = location
        return self.session

    async def get_session_by_token(self, token: str) -> AuthSession | None:
        del token
        return self.session

    async def get_session_by_id(self, session_id):
        return self.session if self.session.id == session_id else None

    async def get_session_by_refresh_token(self, refresh_token: str) -> AuthSession | None:
        del refresh_token
        return self.session

    async def rotate_tokens(
        self,
        session: AuthSession,
        *,
        new_access_token: str,
        new_access_expires_at: datetime,
        new_refresh_token: str,
        new_refresh_expires_at: datetime,
    ) -> AuthSession:
        del new_access_token, new_refresh_token
        session.expires_at = new_access_expires_at
        session.refresh_token_expires_at = new_refresh_expires_at
        return session

    async def list_user_sessions(self, user_id, *, include_expired: bool = False):
        del include_expired
        return [self.session] if self.session.user_id == user_id else []

    async def update_activity(self, token: str) -> bool:
        del token
        return True

    async def mark_current(self, token: str) -> bool:
        del token
        self.session.is_current = True
        return True

    async def revoke_session(self, session_id, user_id) -> bool:
        return self.session.id == session_id and self.session.user_id == user_id

    async def revoke_all_sessions(self, user_id, *, exclude_token_hash: str | None = None) -> int:
        del exclude_token_hash
        return 1 if self.session.user_id == user_id else 0

    async def cleanup_expired(self, *, older_than_days: int = 30) -> int:
        del older_than_days
        return 0


def test_auth_context_coerces_legacy_objects() -> None:
    user_id = uuid4()
    org_id = uuid4()
    ctx = AuthContext(
        user=SimpleNamespace(id=user_id, email="nova@example.com", name="Nova", is_admin=True),
        organization=SimpleNamespace(id=org_id, name="Sibyl", slug="sibyl"),
        org_role="owner",
        scopes=["api:read", "api:write", "api:read"],
    )

    assert isinstance(ctx.user, AuthUser)
    assert isinstance(ctx.organization, AuthOrganization)
    assert ctx.org_role is OrganizationRole.OWNER
    assert ctx.user_id == str(user_id)
    assert ctx.organization_id == str(org_id)
    assert ctx.scopes == frozenset({"api:read", "api:write"})


def test_github_identity_accepts_alias() -> None:
    identity = GitHubUserIdentity.model_validate(
        {
            "id": 42,
            "login": "hyperb1iss",
            "email": "stef@hyperbliss.tech",
        }
    )

    assert identity.github_id == 42
    assert identity.login == "hyperb1iss"


@pytest.mark.asyncio
async def test_auth_repositories_are_runtime_checkable_contracts() -> None:
    user = AuthUser(id=uuid4(), email="nova@example.com", name="Nova", github_id=7)
    organization = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
    membership = AuthMembership(
        id=uuid4(),
        organization_id=organization.id,
        user_id=user.id,
        role=OrganizationRole.ADMIN,
    )
    session = AuthSession(
        id=uuid4(),
        user_id=user.id,
        organization_id=organization.id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    user_repo = FakeUserRepository(user)
    organization_repo = FakeOrganizationRepository(organization)
    membership_repo = FakeOrganizationMembershipRepository(membership)
    session_repo = FakeSessionRepository(session)

    assert isinstance(user_repo, UserRepository)
    assert isinstance(organization_repo, OrganizationRepository)
    assert isinstance(membership_repo, OrganizationMembershipRepository)
    assert isinstance(session_repo, SessionRepository)

    github_identity = GitHubUserIdentity(id=9, login="nova", email="nova@example.com")
    updated = await user_repo.upsert_from_github(github_identity, is_admin=True)
    listed_sessions = await session_repo.list_user_sessions(user.id)

    assert updated.github_id == 9
    assert updated.is_admin is True
    assert await organization_repo.get_by_slug("sibyl") == organization
    assert await membership_repo.get_for_user(organization.id, user.id) == membership
    assert listed_sessions == [session]
