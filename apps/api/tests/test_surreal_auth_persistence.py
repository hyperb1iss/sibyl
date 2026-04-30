from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from sibyl.db.models import ProjectRole
from sibyl.persistence import auth_archive
from sibyl.persistence.auth_archive import restore_auth_archive_payload
from sibyl.persistence.surreal import auth as surreal_auth, auth_runtime as surreal_auth_runtime
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


class _RecordingAuthClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        return self.response


@pytest_asyncio.fixture
async def surreal_auth_client() -> SurrealAuthClient:
    client = SurrealAuthClient(url="memory://")
    await bootstrap_auth_schema(client, reset=True)
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_surreal_auth_repository_replace_uses_single_upsert_statement() -> None:
    user_id = uuid4()
    record = {"uuid": str(user_id), "name": "Nova"}
    client = _RecordingAuthClient([record])
    repo = surreal_auth._SurrealAuthRepository(client)

    saved = await repo._replace("users", uuid=user_id, record=record)

    assert saved["uuid"] == str(user_id)
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "UPSERT users CONTENT $record WHERE uuid = $uuid" in query
    assert "DELETE FROM users" not in query
    assert params == {"uuid": str(user_id), "record": record}


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
async def test_auth_archive_export_reads_from_surreal_backend(
    monkeypatch: pytest.MonkeyPatch,
    surreal_auth_client: SurrealAuthClient,
) -> None:
    repo = SurrealUserRepository.from_client(surreal_auth_client)
    await repo.create_local_user(
        email="export@example.com",
        password="super-secret-password",
        name="Export Nova",
    )
    close = AsyncMock()

    monkeypatch.setattr(auth_archive.config_module.settings, "auth_store", "surreal")
    monkeypatch.setattr(auth_archive, "build_surreal_auth_client", lambda: surreal_auth_client)
    monkeypatch.setattr(surreal_auth_client, "close", close)

    payload = await auth_archive.export_auth_archive_payload()

    assert payload["row_counts"]["users"] == 1
    assert payload["total_rows"] == 1
    assert payload["tables"]["users"][0]["email"] == "export@example.com"
    close.assert_awaited_once()


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
async def test_surreal_auth_context_resolver_batches_request_context_reads() -> None:
    user_id = uuid4()
    organization_id = uuid4()
    membership_id = uuid4()
    client = _RecordingAuthClient(
        {
            "user": {
                "uuid": str(user_id),
                "email": "batched@example.com",
                "name": "Batched",
                "is_admin": False,
            },
            "organization": {
                "uuid": str(organization_id),
                "name": "Batched Org",
                "slug": "batched-org",
                "is_personal": False,
                "settings": {},
            },
            "membership": {
                "uuid": str(membership_id),
                "organization_id": str(organization_id),
                "user_id": str(user_id),
                "role": OrganizationRole.MEMBER.value,
            },
        }
    )
    resolver = SurrealAuthContextResolver.from_client(client)  # type: ignore[arg-type]

    ctx = await resolver.resolve(
        {
            "sub": str(user_id),
            "org": str(organization_id),
            "scopes": ["api:read"],
        }
    )

    assert ctx.user.id == user_id
    assert ctx.organization is not None
    assert ctx.organization.id == organization_id
    assert ctx.org_role is OrganizationRole.MEMBER
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "RETURN" in query
    assert "FROM users" in query
    assert "FROM organizations" in query
    assert "FROM organization_members" in query
    assert params == {
        "user_id": str(user_id),
        "organization_id": str(organization_id),
    }


@pytest.mark.asyncio
async def test_surreal_accessible_projects_batch_query_runs_against_memory_backend(
    surreal_auth_client: SurrealAuthClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    user_id = uuid4()
    visible_project_id = uuid4()
    direct_project_id = uuid4()
    team_project_id = uuid4()
    hidden_project_id = uuid4()
    team_id = uuid4()

    for record in [
        {
            "uuid": str(visible_project_id),
            "organization_id": str(organization_id),
            "graph_project_id": "project_visible",
            "visibility": "org",
        },
        {
            "uuid": str(direct_project_id),
            "organization_id": str(organization_id),
            "graph_project_id": "project_direct",
            "visibility": "private",
        },
        {
            "uuid": str(team_project_id),
            "organization_id": str(organization_id),
            "graph_project_id": "project_team",
            "visibility": "private",
        },
        {
            "uuid": str(hidden_project_id),
            "organization_id": str(organization_id),
            "graph_project_id": "project_hidden",
            "visibility": "private",
        },
    ]:
        await surreal_auth_client.execute_query("CREATE projects CONTENT $record;", record=record)

    await surreal_auth_client.execute_query(
        "CREATE project_members CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "project_id": str(direct_project_id),
            "user_id": str(user_id),
            "role": ProjectRole.VIEWER.value,
        },
    )
    await surreal_auth_client.execute_query(
        "CREATE team_members CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "team_id": str(team_id),
            "user_id": str(user_id),
        },
    )
    await surreal_auth_client.execute_query(
        "CREATE team_projects CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "team_id": str(team_id),
            "project_id": str(team_project_id),
            "role": ProjectRole.MAINTAINER.value,
        },
    )

    @asynccontextmanager
    async def scope():
        yield surreal_auth_client

    monkeypatch.setattr(surreal_auth_runtime, "_auth_client_scope", scope)

    accessible = await surreal_auth_runtime.list_accessible_project_graph_ids(
        SimpleNamespace(
            organization=SimpleNamespace(id=organization_id),
            user=SimpleNamespace(id=user_id),
            org_role=OrganizationRole.MEMBER,
        )
    )

    assert accessible == {"project_visible", "project_direct", "project_team"}
    role = await surreal_auth_runtime.verify_entity_project_access(
        ctx=SimpleNamespace(
            organization=SimpleNamespace(id=organization_id),
            user=SimpleNamespace(id=user_id),
            org_role=OrganizationRole.MEMBER,
        ),
        entity_project_id="project_team",
        required_role=ProjectRole.CONTRIBUTOR,
    )

    assert role is ProjectRole.MAINTAINER


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
