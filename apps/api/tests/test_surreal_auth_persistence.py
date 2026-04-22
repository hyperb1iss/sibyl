from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from sibyl.persistence.auth_archive import restore_auth_archive_payload
from sibyl.persistence.surreal.auth import (
    SurrealAuthContextResolver,
    SurrealOrganizationMembershipRepository,
    SurrealOrganizationRepository,
    SurrealUserRepository,
)
from sibyl_core.auth import GitHubUserIdentity, OrganizationRole, PasswordChange
from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema

pytest.importorskip("surrealdb")


def _normalize_records(result: object) -> list[dict[str, object]]:
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    if not isinstance(result, list):
        return []

    records: list[dict[str, object]] = []
    for item in result:
        if isinstance(item, dict):
            records.append(item)
            continue
        if not isinstance(item, list):
            continue
        for nested in item:
            if isinstance(nested, dict):
                records.append(nested)
    return records


@pytest_asyncio.fixture
async def surreal_auth_client() -> SurrealAuthClient:
    client = SurrealAuthClient(url="memory://")
    await bootstrap_auth_schema(client, reset=True)
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_surreal_user_repository_supports_local_and_github_users(
    surreal_auth_client: SurrealAuthClient,
) -> None:
    repo = SurrealUserRepository.from_client(surreal_auth_client)

    created = await repo.create_local_user(
        email="nova@example.com",
        password="super-secret-password",
        name="Nova",
        is_admin=True,
    )
    authed = await repo.authenticate_local(
        email="nova@example.com",
        password="super-secret-password",
    )
    updated = await repo.update_profile(
        created,
        name="Nova Prime",
        avatar_url="https://example.com/nova.png",
    )
    changed = await repo.change_password(
        updated,
        PasswordChange(
            current_password="super-secret-password",
            new_password="even-more-secret-password",
        ),
    )
    github_user = await repo.upsert_from_github(
        GitHubUserIdentity(
            id=42,
            login="octonova",
            email="octonova@example.com",
            name="Octo Nova",
            avatar_url="https://example.com/octo.png",
        )
    )

    assert authed is not None
    assert authed.id == created.id
    assert updated.name == "Nova Prime"
    assert updated.avatar_url == "https://example.com/nova.png"
    assert changed.id == created.id
    assert await repo.authenticate_local(
        email="nova@example.com",
        password="even-more-secret-password",
    )
    assert github_user.github_id == 42
    assert github_user.email == "octonova@example.com"
    assert await repo.get_by_email("nova@example.com") == changed


@pytest.mark.asyncio
async def test_surreal_org_and_membership_repositories_enforce_owner_invariants(
    surreal_auth_client: SurrealAuthClient,
) -> None:
    user_repo = SurrealUserRepository.from_client(surreal_auth_client)
    org_repo = SurrealOrganizationRepository.from_client(surreal_auth_client)
    membership_repo = SurrealOrganizationMembershipRepository.from_client(surreal_auth_client)

    owner = await user_repo.create_local_user(
        email="owner@example.com",
        password="owner-password",
        name="Owner",
    )
    teammate = await user_repo.upsert_from_github(
        GitHubUserIdentity(
            id=99,
            login="teamie",
            email="teamie@example.com",
            name="Teamie",
        )
    )
    organization = await org_repo.create(name="Nova Org", settings={"theme": "cyan"})
    personal = await org_repo.create_personal_for_user(owner)
    same_personal = await org_repo.create_personal_for_user(owner)

    owner_membership = await membership_repo.add_member(
        organization_id=organization.id,
        user_id=owner.id,
        role=OrganizationRole.OWNER,
    )
    viewer_membership = await membership_repo.add_member(
        organization_id=organization.id,
        user_id=teammate.id,
        role=OrganizationRole.VIEWER,
    )
    promoted = await membership_repo.set_role(
        organization_id=organization.id,
        user_id=teammate.id,
        role=OrganizationRole.ADMIN,
    )
    organizations = await org_repo.list_all()

    assert personal.id == same_personal.id
    assert organization.settings == {"theme": "cyan"}
    assert owner_membership.role is OrganizationRole.OWNER
    assert viewer_membership.role is OrganizationRole.VIEWER
    assert promoted.role is OrganizationRole.ADMIN
    assert {org.slug for org in organizations} == {"nova-org", personal.slug}

    with pytest.raises(ValueError, match="last organization owner"):
        await membership_repo.set_role(
            organization_id=organization.id,
            user_id=owner.id,
            role=OrganizationRole.ADMIN,
        )

    with pytest.raises(ValueError, match="last organization owner"):
        await membership_repo.remove_member(
            organization_id=organization.id,
            user_id=owner.id,
        )


@pytest.mark.asyncio
async def test_surreal_auth_context_resolver_uses_surreal_repositories(
    surreal_auth_client: SurrealAuthClient,
) -> None:
    user_repo = SurrealUserRepository.from_client(surreal_auth_client)
    org_repo = SurrealOrganizationRepository.from_client(surreal_auth_client)
    membership_repo = SurrealOrganizationMembershipRepository.from_client(surreal_auth_client)

    user = await user_repo.create_local_user(
        email="resolver@example.com",
        password="resolver-password",
        name="Resolver",
    )
    organization = await org_repo.create(name="Resolver Org")
    await membership_repo.add_member(
        organization_id=organization.id,
        user_id=user.id,
        role=OrganizationRole.ADMIN,
    )
    resolver = SurrealAuthContextResolver.from_client(surreal_auth_client)

    ctx = await resolver.resolve(
        {
            "sub": str(user.id),
            "org": str(organization.id),
            "scopes": ["api:read", "mcp"],
        }
    )

    assert ctx.user.id == user.id
    assert ctx.organization is not None
    assert ctx.organization.id == organization.id
    assert ctx.org_role is OrganizationRole.ADMIN
    assert ctx.scopes == frozenset({"api:read", "mcp"})


@pytest.mark.asyncio
async def test_auth_archive_restore_accepts_full_user_rows(
    surreal_auth_client: SurrealAuthClient,
) -> None:
    user_id = uuid4()
    organization_id = uuid4()
    membership_id = uuid4()
    payload = {
        "version": "1.0",
        "created_at": "2026-04-21T02:00:00+00:00",
        "tables": {
            "users": [
                {
                    "id": str(user_id),
                    "github_id": None,
                    "email": "restore@example.com",
                    "name": "Restore Nova",
                    "bio": "Recovered from postgres",
                    "timezone": "UTC",
                    "avatar_url": "https://example.com/nova.png",
                    "email_verified_at": "2026-04-20T01:02:03+00:00",
                    "last_login_at": "2026-04-20T04:05:06+00:00",
                    "preferences": {"theme": "cyan"},
                    "password_salt": "salt",
                    "password_hash": "hash",
                    "password_iterations": 600000,
                    "is_admin": True,
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "organizations": [
                {
                    "id": str(organization_id),
                    "name": "Restore Org",
                    "slug": "restore-org",
                    "is_personal": False,
                    "settings": {"theme": "cyan"},
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "organization_members": [
                {
                    "id": str(membership_id),
                    "organization_id": str(organization_id),
                    "user_id": str(user_id),
                    "role": OrganizationRole.OWNER.value,
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
        },
        "row_counts": {
            "users": 1,
            "organizations": 1,
            "organization_members": 1,
        },
        "total_rows": 3,
    }

    with (
        patch.object(surreal_auth_client, "close", AsyncMock()),
        patch(
            "sibyl.persistence.auth_archive.build_surreal_auth_client",
            return_value=surreal_auth_client,
        ),
    ):
        result = await restore_auth_archive_payload(payload, clean=True)

    assert result.success is True
    assert result.tables_restored == 3
    assert result.rows_restored == 3

    user_repo = SurrealUserRepository.from_client(surreal_auth_client)
    membership_repo = SurrealOrganizationMembershipRepository.from_client(surreal_auth_client)
    restored_user = await user_repo.get_by_email("restore@example.com")
    restored_membership = await membership_repo.get_for_user(
        organization_id=organization_id,
        user_id=user_id,
    )
    raw_user = _normalize_records(
        await surreal_auth_client.execute_query(
            "SELECT * FROM users WHERE uuid = $uuid LIMIT 1;",
            uuid=str(user_id),
        )
    )

    assert restored_user is not None
    assert restored_user.id == user_id
    assert restored_user.is_admin is True
    assert restored_membership is not None
    assert restored_membership.role is OrganizationRole.OWNER
    assert raw_user[0]["email_verified_at"] is not None
    assert raw_user[0]["last_login_at"] is not None
