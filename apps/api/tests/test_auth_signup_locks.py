from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sibyl.auth import locks as auth_locks
from sibyl.persistence.legacy import auth as legacy_auth
from sibyl.persistence.surreal import auth_runtime as surreal_auth_runtime
from sibyl_core.auth import GitHubUserIdentity


@pytest.mark.asyncio
async def test_auth_lock_helpers_hash_identity_values(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    @asynccontextmanager
    async def fake_entity_lock(org_id: str, entity_id: str) -> AsyncIterator[str]:
        calls.append((org_id, entity_id))
        yield "lock-token"

    monkeypatch.setattr(auth_locks, "entity_lock", fake_entity_lock)

    async with auth_locks.signup_email_lock("Nova@Example.com "):
        pass
    async with auth_locks.oauth_identity_lock("GitHub", 42):
        pass

    assert calls[0][0] == "auth"
    assert calls[0][1].startswith("signup-email:")
    assert "Nova" not in calls[0][1]
    assert calls[1][1].startswith("oauth:github:")
    assert "42" not in calls[1][1]


@pytest.mark.asyncio
async def test_legacy_local_signup_uses_email_and_bootstrap_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    user_id = uuid4()
    org_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        email="nova@example.com",
        name="Nova",
        github_id=None,
        is_admin=True,
    )
    organization = SimpleNamespace(id=org_id)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[object]:
        yield object()

    @asynccontextmanager
    async def fake_signup_email_lock(email: str) -> AsyncIterator[None]:
        events.append(f"enter-email:{email}")
        yield
        events.append(f"exit-email:{email}")

    @asynccontextmanager
    async def fake_first_user_admin_lock() -> AsyncIterator[None]:
        events.append("enter-bootstrap")
        yield
        events.append("exit-bootstrap")

    class FakeUserManager:
        def __init__(self, session: object) -> None:
            del session

        async def has_any_users(self) -> bool:
            events.append("has-any-users")
            return False

        async def create_local_user(
            self, *, email: str, password: str, name: str, is_admin: bool = False
        ) -> object:
            del password, name
            events.append(f"create-user:{email}:{is_admin}")
            return user

    class FakeOrganizationManager:
        def __init__(self, session: object) -> None:
            del session

        async def create_personal_for_user(self, created_user: object) -> object:
            assert created_user is user
            return organization

    class FakeMembershipManager:
        def __init__(self, session: object) -> None:
            del session

        async def add_member(self, **kwargs: object) -> None:
            assert kwargs["organization_id"] == org_id
            assert kwargs["user_id"] == user_id

    async def fake_issue_auth_session(**kwargs: object) -> object:
        assert kwargs["user"] is user
        assert kwargs["organization"] is organization
        return SimpleNamespace(user=user, organization=organization)

    monkeypatch.setattr("sibyl.db.connection.get_session", lambda: fake_session_scope())
    monkeypatch.setattr(legacy_auth, "signup_email_lock", fake_signup_email_lock)
    monkeypatch.setattr(legacy_auth, "first_user_admin_lock", fake_first_user_admin_lock)
    monkeypatch.setattr(legacy_auth, "UserManager", FakeUserManager)
    monkeypatch.setattr(legacy_auth, "OrganizationManager", FakeOrganizationManager)
    monkeypatch.setattr(legacy_auth, "OrganizationMembershipManager", FakeMembershipManager)
    monkeypatch.setattr(legacy_auth, "_issue_auth_session", fake_issue_auth_session)

    await legacy_auth.signup_legacy_local_user(
        email="nova@example.com",
        password="password",
        name="Nova",
        request=None,
    )

    assert events == [
        "enter-email:nova@example.com",
        "enter-bootstrap",
        "has-any-users",
        "create-user:nova@example.com:True",
        "exit-bootstrap",
        "exit-email:nova@example.com",
    ]


@pytest.mark.asyncio
async def test_legacy_github_login_uses_oauth_and_bootstrap_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    user_id = uuid4()
    org_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        email="nova@example.com",
        name="Nova",
        github_id=42,
        is_admin=True,
    )
    organization = SimpleNamespace(id=org_id)

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[object]:
        yield object()

    @asynccontextmanager
    async def fake_oauth_identity_lock(provider: str, subject: object) -> AsyncIterator[None]:
        events.append(f"enter-oauth:{provider}:{subject}")
        yield
        events.append(f"exit-oauth:{provider}:{subject}")

    @asynccontextmanager
    async def fake_first_user_admin_lock() -> AsyncIterator[None]:
        events.append("enter-bootstrap")
        yield
        events.append("exit-bootstrap")

    class FakeUserManager:
        def __init__(self, session: object) -> None:
            del session

        async def has_any_users(self) -> bool:
            events.append("has-any-users")
            return False

        async def upsert_from_github(
            self, identity: GitHubUserIdentity, *, is_admin: bool = False
        ) -> object:
            events.append(f"upsert-github:{identity.github_id}:{is_admin}")
            return user

    class FakeOrganizationManager:
        def __init__(self, session: object) -> None:
            del session

        async def create_personal_for_user(self, created_user: object) -> object:
            assert created_user is user
            return organization

    class FakeMembershipManager:
        def __init__(self, session: object) -> None:
            del session

        async def add_member(self, **kwargs: object) -> None:
            assert kwargs["organization_id"] == org_id
            assert kwargs["user_id"] == user_id

    async def fake_issue_auth_session(**kwargs: object) -> object:
        assert kwargs["user"] is user
        assert kwargs["organization"] is organization
        return SimpleNamespace(user=user, organization=organization)

    monkeypatch.setattr("sibyl.db.connection.get_session", lambda: fake_session_scope())
    monkeypatch.setattr(legacy_auth, "oauth_identity_lock", fake_oauth_identity_lock)
    monkeypatch.setattr(legacy_auth, "first_user_admin_lock", fake_first_user_admin_lock)
    monkeypatch.setattr(legacy_auth, "UserManager", FakeUserManager)
    monkeypatch.setattr(legacy_auth, "OrganizationManager", FakeOrganizationManager)
    monkeypatch.setattr(legacy_auth, "OrganizationMembershipManager", FakeMembershipManager)
    monkeypatch.setattr(legacy_auth, "_issue_auth_session", fake_issue_auth_session)

    await legacy_auth.login_legacy_github_identity(
        identity=GitHubUserIdentity(
            id=42,
            login="octonova",
            email="nova@example.com",
            name="Nova",
        ),
        request=None,
    )

    assert events == [
        "enter-oauth:github:42",
        "enter-bootstrap",
        "has-any-users",
        "upsert-github:42:True",
        "exit-bootstrap",
        "exit-oauth:github:42",
    ]


@pytest.mark.asyncio
async def test_surreal_local_signup_uses_email_and_bootstrap_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    user = SimpleNamespace(
        id=uuid4(),
        email="nova@example.com",
        name="Nova",
        avatar_url=None,
        github_id=None,
        is_admin=True,
        bio=None,
        timezone="UTC",
        preferences={},
    )
    organization = SimpleNamespace(id=uuid4(), slug="nova")

    @asynccontextmanager
    async def fake_auth_client_scope() -> AsyncIterator[object]:
        yield object()

    @asynccontextmanager
    async def fake_signup_email_lock(email: str) -> AsyncIterator[None]:
        events.append(f"enter-email:{email}")
        yield
        events.append(f"exit-email:{email}")

    @asynccontextmanager
    async def fake_first_user_admin_lock() -> AsyncIterator[None]:
        events.append("enter-bootstrap")
        yield
        events.append("exit-bootstrap")

    class FakeUserRepository:
        @classmethod
        def from_client(cls, client: object) -> FakeUserRepository:
            del client
            return cls()

        async def has_any_users(self) -> bool:
            events.append("has-any-users")
            return False

        async def create_local_user(
            self, *, email: str, password: str, name: str, is_admin: bool = False
        ) -> object:
            del password, name
            events.append(f"create-user:{email}:{is_admin}")
            return user

    async def fake_issue_auth_session(*args: object, **kwargs: object) -> object:
        del args
        assert kwargs["organization"] is organization
        return SimpleNamespace(user=user, organization=organization)

    async def fake_ensure_personal_org_membership_record(
        client: object, created_user: object
    ) -> dict[str, object]:
        del client
        assert created_user is user
        return {"uuid": str(organization.id), "slug": organization.slug}

    monkeypatch.setattr(surreal_auth_runtime, "_auth_client_scope", fake_auth_client_scope)
    monkeypatch.setattr(surreal_auth_runtime, "signup_email_lock", fake_signup_email_lock)
    monkeypatch.setattr(surreal_auth_runtime, "first_user_admin_lock", fake_first_user_admin_lock)
    monkeypatch.setattr(surreal_auth_runtime, "SurrealUserRepository", FakeUserRepository)
    monkeypatch.setattr(
        surreal_auth_runtime,
        "_ensure_personal_org_membership_record",
        fake_ensure_personal_org_membership_record,
    )
    monkeypatch.setattr(surreal_auth_runtime, "_auth_org_namespace", lambda _record: organization)
    monkeypatch.setattr(surreal_auth_runtime, "_issue_auth_session", fake_issue_auth_session)

    await surreal_auth_runtime.signup_local_user(
        email="nova@example.com",
        password="password",
        name="Nova",
        request=None,
    )

    assert events == [
        "enter-email:nova@example.com",
        "enter-bootstrap",
        "has-any-users",
        "create-user:nova@example.com:True",
        "exit-bootstrap",
        "exit-email:nova@example.com",
    ]
