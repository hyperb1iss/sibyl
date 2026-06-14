from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from sibyl.persistence import auth_archive
from sibyl.persistence.auth_archive import restore_auth_archive_payload
from sibyl.persistence.surreal import auth as surreal_auth, auth_runtime as surreal_auth_runtime
from sibyl.persistence.surreal.auth import (
    SurrealAuthContextResolver,
    SurrealOrganizationMembershipRepository,
    SurrealOrganizationRepository,
    SurrealUserRepository,
)
from sibyl_core.auth import GitHubUserIdentity, OrganizationRole, PasswordChange, ProjectRole
from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema
from sibyl_core.backends.surreal.auth_schema import AUTH_TABLES

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
async def test_surreal_user_repository_burns_password_check_for_missing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SurrealUserRepository.from_client(_RecordingAuthClient([]))  # type: ignore[arg-type]
    burn = MagicMock()
    monkeypatch.setattr(surreal_auth, "verify_password_timing_floor", burn)

    result = await repo.authenticate_local(
        email="missing@example.com",
        password="candidate-password",
    )

    assert result is None
    burn.assert_called_once_with(
        "candidate-password",
        iterations=surreal_auth.config_module.settings.password_iterations,
    )


@pytest.mark.asyncio
async def test_surreal_user_repository_burns_password_check_for_empty_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RecordingAuthClient([])
    repo = SurrealUserRepository.from_client(client)  # type: ignore[arg-type]
    burn = MagicMock()
    monkeypatch.setattr(surreal_auth, "verify_password_timing_floor", burn)

    result = await repo.authenticate_local(
        email="nova@example.com",
        password="",
    )

    assert result is None
    assert client.calls == []
    burn.assert_called_once_with(
        "",
        iterations=surreal_auth.config_module.settings.password_iterations,
    )


@pytest.mark.asyncio
async def test_auth_schema_migration_rejects_invalid_role_values() -> None:
    client = SurrealAuthClient(url="memory://")
    try:
        await client.execute_query(
            """
            CREATE schema_version:auth SET
                name = 'auth',
                version = 3,
                migrations = [
                    { version: 1, name: 'auth_schema_bootstrap' },
                    { version: 2, name: 'auth_invitation_token_hash_cleanup' },
                    { version: 3, name: 'auth_project_slug_lookup_cleanup' }
                ],
                created_at = time::now(),
                updated_at = time::now();
            """
        )
        await client.execute_query(
            "CREATE organization_members CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(uuid4()),
                "user_id": str(uuid4()),
                "role": "super_admin",
            },
        )

        with pytest.raises(RuntimeError, match=r"organization_members\.role enum assertion"):
            await bootstrap_auth_schema(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_schema_assertions_reject_invalid_writes(
    surreal_auth_client: SurrealAuthClient,
) -> None:
    with pytest.raises(Exception, match=r"visibility|internet"):
        await surreal_auth_client.execute_query(
            "CREATE projects CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(uuid4()),
                "name": "Bad Project",
                "slug": "bad-project",
                "graph_project_id": "bad-project",
                "visibility": "internet",
                "default_role": ProjectRole.VIEWER.value,
            },
        )


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

    monkeypatch.setattr(auth_archive, "build_surreal_auth_client", lambda: surreal_auth_client)
    monkeypatch.setattr(surreal_auth_client, "close", close)

    payload = await auth_archive.export_auth_archive_payload()

    assert payload["row_counts"]["users"] == 1
    assert payload["total_rows"] == 1
    assert payload["tables"]["users"][0]["email"] == "export@example.com"
    close.assert_awaited_once()


def test_auth_archive_tables_cover_auth_schema_tables() -> None:
    assert set(auth_archive.AUTH_ARCHIVE_TABLES) == set(AUTH_TABLES)


@pytest.mark.asyncio
async def test_auth_archive_export_can_scope_to_one_organization(
    monkeypatch: pytest.MonkeyPatch,
    surreal_auth_client: SurrealAuthClient,
) -> None:
    org_a = uuid4()
    org_b = uuid4()
    user_a = uuid4()
    user_b = uuid4()
    api_key_a = uuid4()
    api_key_b = uuid4()
    session_a = uuid4()
    session_b = uuid4()
    invitation_a = uuid4()
    invitation_b = uuid4()
    device_request_a = uuid4()
    device_request_b = uuid4()
    project_a = uuid4()
    project_b = uuid4()

    for user_id, email in ((user_a, "a@example.com"), (user_b, "b@example.com")):
        await surreal_auth_client.execute_query(
            "CREATE users CONTENT $record;",
            record={
                "uuid": str(user_id),
                "email": email,
                "name": email,
                "password_salt": f"{email}-salt",
                "password_hash": f"{email}-hash",
                "password_iterations": 310000,
            },
        )
    for user_id, email in ((user_a, "a@example.com"), (user_b, "b@example.com")):
        await surreal_auth_client.execute_query(
            "CREATE user_identity CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "provider_name": "oidc",
                "issuer": "https://idp.example.com",
                "subject": str(user_id),
                "subject_key": f"oidc:{user_id}",
                "user_id": str(user_id),
                "email": email,
            },
        )
        await surreal_auth_client.execute_query(
            "CREATE password_reset_tokens CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "user_id": str(user_id),
                "token_hash": f"{email}-reset-token-hash",
                "expires_at": datetime(2026, 4, 20, 1, 2, 3, tzinfo=UTC),
            },
        )
        await surreal_auth_client.execute_query(
            "CREATE login_history CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "user_id": str(user_id),
                "event_type": "login",
                "success": True,
                "ip_address": "192.0.2.10",
            },
        )
        await surreal_auth_client.execute_query(
            "CREATE oauth_connections CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "user_id": str(user_id),
                "provider": "github",
                "provider_user_id": str(user_id),
                "access_token_encrypted": f"{email}-access-token",
                "refresh_token_encrypted": f"{email}-refresh-token",
            },
        )
    for org_id, slug in ((org_a, "org-a"), (org_b, "org-b")):
        await surreal_auth_client.execute_query(
            "CREATE organizations CONTENT $record;",
            record={"uuid": str(org_id), "name": slug, "slug": slug},
        )
    for org_id, user_id in ((org_a, user_a), (org_b, user_b)):
        await surreal_auth_client.execute_query(
            "CREATE organization_members CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(org_id),
                "user_id": str(user_id),
                "role": "owner",
            },
        )
    for org_id, user_id, session_id, suffix in (
        (org_a, user_a, session_a, "a"),
        (org_b, user_b, session_b, "b"),
    ):
        await surreal_auth_client.execute_query(
            "CREATE user_sessions CONTENT $record;",
            record={
                "uuid": str(session_id),
                "organization_id": str(org_id),
                "user_id": str(user_id),
                "token_hash": f"{suffix}-session-token-hash",
                "refresh_token_hash": f"{suffix}-refresh-token-hash",
                "expires_at": datetime(2026, 4, 20, 1, 2, 3, tzinfo=UTC),
            },
        )
    for org_id, creator_id, invitation_id, suffix in (
        (org_a, user_a, invitation_a, "a"),
        (org_b, user_b, invitation_b, "b"),
    ):
        await surreal_auth_client.execute_query(
            "CREATE organization_invitations CONTENT $record;",
            record={
                "uuid": str(invitation_id),
                "organization_id": str(org_id),
                "invited_email": f"{suffix}@example.com",
                "created_by_user_id": str(creator_id),
                "token": f"{suffix}-invite-token",
                "token_hash": f"{suffix}-invite-token-hash",
            },
        )
    for org_id, user_id, request_id, suffix in (
        (org_a, user_a, device_request_a, "a"),
        (org_b, user_b, device_request_b, "b"),
    ):
        await surreal_auth_client.execute_query(
            "CREATE device_authorization_requests CONTENT $record;",
            record={
                "uuid": str(request_id),
                "organization_id": str(org_id),
                "user_id": str(user_id),
                "device_code_hash": f"{suffix}-device-code-hash",
                "user_code": f"{suffix}-user-code",
                "expires_at": datetime(2026, 4, 20, 1, 2, 3, tzinfo=UTC),
            },
        )
    for org_id, project_id, owner_id, slug in (
        (org_a, project_a, user_a, "project-a"),
        (org_b, project_b, user_b, "project-b"),
    ):
        await surreal_auth_client.execute_query(
            "CREATE projects CONTENT $record;",
            record={
                "uuid": str(project_id),
                "organization_id": str(org_id),
                "name": slug,
                "slug": slug,
                "graph_project_id": str(project_id),
                "owner_user_id": str(owner_id),
            },
        )
    for org_id, api_key_id, user_id, prefix in (
        (org_a, api_key_a, user_a, "ak_a"),
        (org_b, api_key_b, user_b, "ak_b"),
    ):
        await surreal_auth_client.execute_query(
            "CREATE api_keys CONTENT $record;",
            record={
                "uuid": str(api_key_id),
                "organization_id": str(org_id),
                "user_id": str(user_id),
                "name": prefix,
                "key_prefix": prefix,
                "key_salt": f"{prefix}_salt",
                "key_hash": f"{prefix}_hash",
            },
        )
    for api_key_id, project_id in ((api_key_a, project_a), (api_key_b, project_b)):
        await surreal_auth_client.execute_query(
            "CREATE api_key_project_scopes CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "api_key_id": str(api_key_id),
                "project_id": str(project_id),
            },
        )

    close = AsyncMock()
    monkeypatch.setattr(auth_archive, "build_surreal_auth_client", lambda: surreal_auth_client)
    monkeypatch.setattr(surreal_auth_client, "close", close)

    payload = await auth_archive.export_auth_archive_payload(organization_id=org_a)

    assert payload["organization_id"] == str(org_a)
    assert [row["uuid"] for row in payload["tables"]["organizations"]] == [str(org_a)]
    assert [row["uuid"] for row in payload["tables"]["users"]] == [str(user_a)]
    assert payload["tables"]["users"][0]["email"] == "a@example.com"
    assert "password_salt" not in payload["tables"]["users"][0]
    assert "password_hash" not in payload["tables"]["users"][0]
    assert "password_iterations" not in payload["tables"]["users"][0]
    assert payload["tables"]["user_identity"] == []
    assert payload["tables"]["password_reset_tokens"] == []
    assert payload["tables"]["login_history"] == []
    assert payload["tables"]["oauth_connections"] == []
    assert [row["uuid"] for row in payload["tables"]["api_keys"]] == [str(api_key_a)]
    assert payload["tables"]["api_keys"][0]["key_prefix"] == "ak_a"
    assert "key_salt" not in payload["tables"]["api_keys"][0]
    assert "key_hash" not in payload["tables"]["api_keys"][0]
    assert [row["uuid"] for row in payload["tables"]["user_sessions"]] == [str(session_a)]
    assert "token_hash" not in payload["tables"]["user_sessions"][0]
    assert "refresh_token_hash" not in payload["tables"]["user_sessions"][0]
    assert [row["uuid"] for row in payload["tables"]["organization_invitations"]] == [
        str(invitation_a)
    ]
    assert "token" not in payload["tables"]["organization_invitations"][0]
    assert "token_hash" not in payload["tables"]["organization_invitations"][0]
    assert [row["uuid"] for row in payload["tables"]["device_authorization_requests"]] == [
        str(device_request_a)
    ]
    assert "device_code_hash" not in payload["tables"]["device_authorization_requests"][0]
    assert "user_code" not in payload["tables"]["device_authorization_requests"][0]
    assert [row["api_key_id"] for row in payload["tables"]["api_key_project_scopes"]] == [
        str(api_key_a)
    ]
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_archive_clean_restore_scopes_to_payload_organization(
    surreal_auth_client: SurrealAuthClient,
) -> None:
    org_a = uuid4()
    org_b = uuid4()
    user_a = uuid4()
    user_b = uuid4()
    old_membership_a = uuid4()
    membership_b = uuid4()
    restored_membership_a = uuid4()

    for user_id, email in ((user_a, "a@example.com"), (user_b, "b@example.com")):
        await surreal_auth_client.execute_query(
            "CREATE users CONTENT $record;",
            record={"uuid": str(user_id), "email": email, "name": email},
        )
    for org_id, slug in ((org_a, "org-a-old"), (org_b, "org-b")):
        await surreal_auth_client.execute_query(
            "CREATE organizations CONTENT $record;",
            record={"uuid": str(org_id), "name": slug, "slug": slug},
        )
    for org_id, user_id, membership_id in (
        (org_a, user_a, old_membership_a),
        (org_b, user_b, membership_b),
    ):
        await surreal_auth_client.execute_query(
            "CREATE organization_members CONTENT $record;",
            record={
                "uuid": str(membership_id),
                "organization_id": str(org_id),
                "user_id": str(user_id),
                "role": "owner",
            },
        )

    payload = {
        "version": "1.0",
        "created_at": "2026-06-01T00:00:00+00:00",
        "organization_id": str(org_a),
        "tables": {
            "organizations": [
                {
                    "id": str(org_a),
                    "name": "org-a-restored",
                    "slug": "org-a-restored",
                    "is_personal": False,
                    "settings": {},
                }
            ],
            "organization_members": [
                {
                    "id": str(restored_membership_a),
                    "organization_id": str(org_a),
                    "user_id": str(user_a),
                    "role": "admin",
                }
            ],
        },
        "row_counts": {"organizations": 1, "organization_members": 1},
        "total_rows": 2,
    }

    with (
        patch.object(surreal_auth_client, "close", AsyncMock()),
        patch(
            "sibyl.persistence.auth_archive.build_surreal_auth_client",
            return_value=surreal_auth_client,
        ),
    ):
        result = await restore_auth_archive_payload(payload, clean=True)

    org_a_rows = _normalize_records(
        await surreal_auth_client.execute_query(
            "SELECT * FROM organizations WHERE uuid = $uuid LIMIT 1;",
            uuid=str(org_a),
        )
    )
    org_b_members = _normalize_records(
        await surreal_auth_client.execute_query(
            "SELECT * FROM organization_members WHERE organization_id = $organization_id;",
            organization_id=str(org_b),
        )
    )
    old_members = _normalize_records(
        await surreal_auth_client.execute_query(
            "SELECT * FROM organization_members WHERE uuid = $uuid;",
            uuid=str(old_membership_a),
        )
    )

    assert result.success is True
    assert org_a_rows[0]["slug"] == "org-a-restored"
    assert [row["uuid"] for row in org_b_members] == [str(membership_b)]
    assert old_members == []


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
        second_result = await restore_auth_archive_payload(payload, clean=False)

    assert result.success is True
    assert result.tables_restored == 3
    assert result.rows_restored == 3
    assert second_result.success is True
    assert second_result.tables_restored == 0
    assert second_result.rows_restored == 0

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
