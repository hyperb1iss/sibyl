from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from sibyl.persistence.legacy.auth import (
    InvalidAuthClaimsError,
    LegacyAuthContextResolver,
    LegacyUserRepository,
    UserNotFoundError,
    list_legacy_user_organizations,
    rotate_legacy_refresh_session_record,
)
from sibyl_core.auth import (
    AuthMembership,
    AuthOrganization,
    AuthUser,
    GitHubUserIdentity,
    OrganizationRole,
)


class FakeUserManager:
    def __init__(self, user: object | None) -> None:
        self.user = user

    async def get_by_id(self, user_id):
        if self.user is None or getattr(self.user, "id", None) != user_id:
            return None
        return self.user

    async def has_any_users(self) -> bool:
        return self.user is not None

    async def get_by_github_id(self, github_id: int):
        if self.user is None or getattr(self.user, "github_id", None) != github_id:
            return None
        return self.user

    async def get_by_email(self, email: str):
        if self.user is None or getattr(self.user, "email", None) != email:
            return None
        return self.user

    async def upsert_from_github(self, identity: GitHubUserIdentity, *, is_admin: bool = False):
        del is_admin
        return SimpleNamespace(
            id=uuid4(),
            github_id=identity.github_id,
            email=identity.email,
            name=identity.name or identity.login,
        )

    async def create_local_user(self, *, email: str, password: str, name: str, is_admin: bool = False):
        del password, is_admin
        return SimpleNamespace(id=uuid4(), email=email, name=name)

    async def authenticate_local(self, *, email: str, password: str):
        del password
        return self.user if self.user is not None and getattr(self.user, "email", None) == email else None

    async def update_profile(self, user, **kwargs):
        for key, value in kwargs.items():
            if value is not None:
                setattr(user, key, value)
        return user

    async def change_password(self, user, change):
        del change
        return user


class FakeOrgRepository:
    def __init__(self, organization: AuthOrganization | None) -> None:
        self.organization = organization

    async def get_by_id(self, org_id):
        if self.organization is None or self.organization.id != org_id:
            return None
        return self.organization


class FakeMembershipRepository:
    def __init__(self, membership: AuthMembership | None) -> None:
        self.membership = membership

    async def get_for_user(self, organization_id, user_id):
        if self.membership is None:
            return None
        if self.membership.organization_id == organization_id and self.membership.user_id == user_id:
            return self.membership
        return None


@pytest.mark.asyncio
async def test_legacy_user_repository_coerces_manager_rows() -> None:
    user_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        email="nova@example.com",
        name="Nova",
        github_id=42,
        is_admin=True,
    )
    repo = LegacyUserRepository(FakeUserManager(user))

    result = await repo.get_by_id(user_id)

    assert result == AuthUser(
        id=user_id,
        email="nova@example.com",
        name="Nova",
        github_id=42,
        is_admin=True,
        timezone="UTC",
    )


@pytest.mark.asyncio
async def test_auth_context_resolver_uses_legacy_repositories() -> None:
    user = AuthUser(id=uuid4(), email="nova@example.com", name="Nova")
    organization = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
    membership = AuthMembership(
        id=uuid4(),
        organization_id=organization.id,
        user_id=user.id,
        role=OrganizationRole.ADMIN,
    )
    resolver = LegacyAuthContextResolver(
        users=LegacyUserRepository(FakeUserManager(user)),
        organizations=FakeOrgRepository(organization),
        memberships=FakeMembershipRepository(membership),
    )

    ctx = await resolver.resolve(
        {
            "sub": str(user.id),
            "org": str(organization.id),
            "scopes": ["api:read"],
        }
    )

    assert ctx.user == user
    assert ctx.organization == organization
    assert ctx.org_role is OrganizationRole.ADMIN
    assert ctx.scopes == frozenset({"api:read"})


@pytest.mark.asyncio
async def test_auth_context_resolver_rejects_bad_subject() -> None:
    resolver = LegacyAuthContextResolver(
        users=LegacyUserRepository(FakeUserManager(None)),
        organizations=FakeOrgRepository(None),
        memberships=FakeMembershipRepository(None),
    )

    with pytest.raises(InvalidAuthClaimsError, match="Invalid token"):
        await resolver.resolve({"sub": "not-a-uuid"})


@pytest.mark.asyncio
async def test_auth_context_resolver_rejects_missing_user() -> None:
    resolver = LegacyAuthContextResolver(
        users=LegacyUserRepository(FakeUserManager(None)),
        organizations=FakeOrgRepository(None),
        memberships=FakeMembershipRepository(None),
    )

    with pytest.raises(UserNotFoundError, match="User not found"):
        await resolver.resolve({"sub": str(uuid4())})


@pytest.mark.asyncio
async def test_list_legacy_user_organizations_uses_relational_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = SimpleNamespace(id=uuid4(), name="Sibyl", slug="sibyl", is_personal=True)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [organization]
    session = AsyncMock()
    session.execute.return_value = result

    @asynccontextmanager
    async def fake_session():
        yield session

    monkeypatch.setattr("sibyl.db.connection.get_session", fake_session)

    organizations = await list_legacy_user_organizations(user_id=uuid4())

    assert organizations == [organization]


@pytest.mark.asyncio
async def test_rotate_legacy_refresh_session_record_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AsyncMock()
    manager.get_session_by_refresh_token.return_value = None
    session = object()

    @asynccontextmanager
    async def fake_session():
        yield session

    monkeypatch.setattr("sibyl.db.connection.get_session", fake_session)
    monkeypatch.setattr("sibyl.persistence.legacy.auth.SessionManager", lambda _: manager)

    rotated = await rotate_legacy_refresh_session_record(
        "refresh-token",
        new_access_token="access",
        new_access_expires_at=object(),
        new_refresh_token="refresh-2",
        new_refresh_expires_at=object(),
    )

    assert rotated is None
