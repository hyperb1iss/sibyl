from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from sibyl.db.models import OrganizationRole, ProjectRole
from sibyl.persistence import organization_common, organization_runtime
from sibyl.persistence.legacy import (
    orgs as legacy_orgs_runtime,
    project_members as legacy_project_members_runtime,
)
from sibyl.persistence.surreal import organization_runtime as surreal_organization_runtime


def _request(*, authorization: str | None = "Bearer current-token") -> SimpleNamespace:
    headers: dict[str, str] = {}
    if authorization is not None:
        headers["authorization"] = authorization
    return SimpleNamespace(
        headers=headers,
        cookies={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def test_organization_runtime_only_exports_neutral_runtime_surface() -> None:
    assert not hasattr(organization_runtime, "create_legacy_org")
    assert not hasattr(organization_runtime, "list_legacy_orgs")
    assert not hasattr(organization_runtime, "can_manage_legacy_project_members")
    assert hasattr(legacy_orgs_runtime, "list_orgs")
    assert hasattr(legacy_project_members_runtime, "list_project_members")
    for legacy_name in [
        "list_legacy_orgs",
        "list_legacy_org_ids",
        "create_legacy_org",
        "get_legacy_org",
        "switch_legacy_org",
        "update_legacy_org",
        "delete_legacy_org",
        "list_legacy_org_members",
        "add_legacy_org_member",
        "update_legacy_org_member_role",
        "remove_legacy_org_member",
        "list_legacy_org_invitations",
        "create_legacy_org_invitation",
        "delete_legacy_org_invitation",
        "accept_legacy_org_invitation",
        "list_legacy_project_members",
        "add_legacy_project_member",
        "update_legacy_project_member_role",
        "remove_legacy_project_member",
    ]:
        assert not hasattr(surreal_organization_runtime, legacy_name)
    assert organization_common.__all__ == [
        "InvitationAcceptance",
        "InvitationRecord",
        "OrgAuthResult",
        "OrgMemberChange",
        "OrgRoleResult",
        "OrgSummary",
        "ProjectMemberChange",
        "ProjectMembersResult",
        "can_manage_project_members",
    ]
    assert not hasattr(organization_common, "LegacyOrgSummary")
    assert not hasattr(organization_common, "LegacyProjectMembersResult")


def test_organization_runtime_resolves_postgres_neutral_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(organization_runtime.settings, "auth_store", "postgres")

    assert (
        organization_runtime._resolve_backend_export("list_orgs") is legacy_orgs_runtime.list_orgs
    )
    assert (
        organization_runtime._resolve_backend_export("list_project_members")
        is legacy_project_members_runtime.list_project_members
    )


@pytest.mark.asyncio
async def test_organization_runtime_dispatches_neutral_org_reads_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(organization_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(
        surreal_organization_runtime,
        "list_orgs",
        dispatched,
    )

    result = await organization_runtime.list_orgs(user_id=uuid4())
    assert result is expected
    dispatched.assert_awaited_once()


@pytest.mark.asyncio
async def test_organization_runtime_dispatches_neutral_org_id_reads_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ["org-1", "org-2"]
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(organization_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(
        surreal_organization_runtime,
        "list_org_ids",
        dispatched,
    )

    result = await organization_runtime.list_org_ids()

    assert result == expected
    dispatched.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_organization_runtime_dispatches_neutral_org_delete_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched = AsyncMock()

    monkeypatch.setattr(organization_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(
        surreal_organization_runtime,
        "delete_org",
        dispatched,
    )

    request = _request()
    user_id = uuid4()

    await organization_runtime.delete_org(
        request=request,
        slug="electric-coven",
        user_id=user_id,
    )

    dispatched.assert_awaited_once_with(
        request=request,
        slug="electric-coven",
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_surreal_delete_org_auth_children_batches_dependent_deletes() -> None:
    org_id = uuid4()
    team_ids = [uuid4(), uuid4()]
    api_key_ids = [uuid4(), uuid4()]
    responses = [
        [{"uuid": str(team_id)} for team_id in team_ids],
        [{"uuid": str(api_key_id)} for api_key_id in api_key_ids],
        [],
        [],
    ]
    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        async def execute_query(self, query: str, **kwargs: object) -> object:
            calls.append((query, kwargs))
            return responses.pop(0)

    await surreal_organization_runtime._delete_org_auth_child_records(
        Client(),
        organization_id=org_id,
    )

    delete_calls = [(query, params) for query, params in calls if query.startswith("DELETE")]
    assert delete_calls == [
        (
            "DELETE FROM team_members WHERE team_id IN $team_ids;",
            {"team_ids": [str(team_id) for team_id in team_ids]},
        ),
        (
            "DELETE FROM api_key_project_scopes WHERE api_key_id IN $api_key_ids;",
            {"api_key_ids": [str(api_key_id) for api_key_id in api_key_ids]},
        ),
    ]


@pytest.mark.asyncio
async def test_organization_runtime_dispatches_neutral_project_member_reads_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
    dispatched = AsyncMock(return_value=expected)
    actor = SimpleNamespace(id=uuid4())
    org_id = uuid4()

    monkeypatch.setattr(organization_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(
        surreal_organization_runtime,
        "list_project_members",
        dispatched,
    )

    result = await organization_runtime.list_project_members(
        project_id="project_123",
        actor=actor,
        org_id=org_id,
    )

    assert result is expected
    dispatched.assert_awaited_once_with(
        project_id="project_123",
        actor=actor,
        org_id=org_id,
    )


@pytest.mark.asyncio
async def test_surreal_list_orgs_materializes_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_a = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000111"),
        slug="alpha",
        name="Alpha",
        is_personal=False,
    )
    org_b = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000222"),
        slug="zeta",
        name="Zeta",
        is_personal=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "FROM organization_members" in query:
                return [
                    {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_b.id),
                        "user_id": str(user_id),
                        "role": OrganizationRole.VIEWER.value,
                    },
                    {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_a.id),
                        "user_id": str(user_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                ]
            if "FROM organizations" in query:
                return [
                    {
                        "uuid": str(org_a.id),
                        "slug": org_a.slug,
                        "name": org_a.name,
                        "is_personal": org_a.is_personal,
                    },
                    {
                        "uuid": str(org_b.id),
                        "slug": org_b.slug,
                        "name": org_b.name,
                        "is_personal": org_b.is_personal,
                    },
                ]
            raise AssertionError(query)

        async def close(self) -> None:
            return None

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    result = await surreal_organization_runtime.list_orgs(user_id=user_id)

    assert [org.slug for org in result] == ["alpha", "zeta"]
    assert result[0].role == OrganizationRole.OWNER
    assert result[1].role == OrganizationRole.VIEWER
    assert len(fake_client.calls) == 2
    assert "FROM organization_members" in fake_client.calls[0][0]
    assert fake_client.calls[0][1] == {"user_id": str(user_id)}
    assert "FROM organizations" in fake_client.calls[1][0]
    assert fake_client.calls[1][1] == {"organization_ids": [str(org_b.id), str(org_a.id)]}


@pytest.mark.asyncio
async def test_surreal_list_org_ids_uses_repository_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organizations = [
        SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
        SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222")),
    ]

    class FakeClient:
        async def close(self) -> None:
            return None

    @asynccontextmanager
    async def fake_scope():
        yield FakeClient()

    org_repo = SimpleNamespace(list_all=AsyncMock(return_value=organizations))

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationRepository,
        "from_client",
        lambda _client: org_repo,
    )

    result = await surreal_organization_runtime.list_org_ids()

    org_repo.list_all.assert_awaited_once_with(limit=100_000)
    assert result == [str(org.id) for org in organizations]


@pytest.mark.asyncio
async def test_surreal_create_org_rotates_current_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    organization = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000321"),
        slug="electric-coven",
        name="Electric Coven",
    )
    refresh_expires = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

    class FakeClient:
        async def close(self) -> None:
            return None

    @asynccontextmanager
    async def fake_scope():
        yield FakeClient()

    org_repo = SimpleNamespace(
        get_by_slug=AsyncMock(return_value=None),
        create=AsyncMock(return_value=organization),
    )
    membership_repo = SimpleNamespace(add_member=AsyncMock())
    session_repo = SimpleNamespace(
        get_session_by_token=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
        rotate_tokens=AsyncMock(),
        create_session=AsyncMock(),
    )
    ensure_indexes = AsyncMock()
    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationRepository,
        "from_client",
        lambda _client: org_repo,
    )
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationMembershipRepository,
        "from_client",
        lambda _client: membership_repo,
    )
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealSessionRepository,
        "from_client",
        lambda _client: session_repo,
    )
    monkeypatch.setattr(
        surreal_organization_runtime,
        "ensure_graph_indexes",
        ensure_indexes,
    )
    monkeypatch.setattr(
        surreal_organization_runtime,
        "create_access_token",
        lambda **_kwargs: "access-token",
    )
    monkeypatch.setattr(
        surreal_organization_runtime,
        "create_refresh_token",
        lambda **_kwargs: ("refresh-token", refresh_expires),
    )
    monkeypatch.setattr(
        surreal_organization_runtime,
        "select_access_token",
        lambda **_kwargs: "current-token",
    )
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)

    result = await surreal_organization_runtime.create_org(
        request=_request(),
        user_id=user_id,
        name="Electric Coven",
    )

    org_repo.create.assert_awaited_once_with(
        name="Electric Coven",
        slug="electric-coven",
        is_personal=False,
    )
    membership_repo.add_member.assert_awaited_once_with(
        organization_id=organization.id,
        user_id=user_id,
        role=OrganizationRole.OWNER,
    )
    ensure_indexes.assert_awaited_once_with(str(organization.id))
    session_repo.rotate_tokens.assert_awaited_once()
    session_repo.create_session.assert_not_awaited()
    assert result.id == organization.id
    assert result.access_token == "access-token"


@pytest.mark.asyncio
async def test_surreal_update_org_uses_update_result_without_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    organization = SimpleNamespace(
        id=uuid4(),
        slug="old-name",
        name="Old Name",
        is_personal=False,
    )

    class FakeClient:
        async def execute_query(self, query: str, **_params):
            if query.startswith("UPDATE organizations"):
                return [
                    {
                        "uuid": str(organization.id),
                        "slug": "new-name",
                        "name": "New Name",
                        "is_personal": False,
                    }
                ]
            return []

    @asynccontextmanager
    async def fake_scope():
        yield FakeClient()

    org_repo = SimpleNamespace(
        get_by_slug=AsyncMock(side_effect=[organization, None]),
        get_by_id=AsyncMock(side_effect=AssertionError("unexpected organization reload")),
    )
    membership_repo = SimpleNamespace(
        get_for_user=AsyncMock(return_value=SimpleNamespace(role=OrganizationRole.ADMIN))
    )
    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationRepository,
        "from_client",
        lambda _client: org_repo,
    )
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationMembershipRepository,
        "from_client",
        lambda _client: membership_repo,
    )
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)

    result = await surreal_organization_runtime.update_org(
        request=_request(),
        slug="old-name",
        user_id=user_id,
        name="New Name",
        new_slug="new-name",
    )

    assert result.id == organization.id
    assert result.slug == "new-name"
    assert result.name == "New Name"
    assert result.role == OrganizationRole.ADMIN
    org_repo.get_by_id.assert_not_awaited()
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_surreal_remove_org_member_allows_self_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid4()
    org = SimpleNamespace(id=uuid4(), slug="electric-coven")

    @asynccontextmanager
    async def fake_scope():
        yield SimpleNamespace(close=AsyncMock())

    org_repo = SimpleNamespace(get_by_slug=AsyncMock(return_value=org))
    membership_repo = SimpleNamespace(
        get_for_user=AsyncMock(return_value=SimpleNamespace(role=OrganizationRole.VIEWER)),
        remove_member=AsyncMock(),
    )
    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationRepository,
        "from_client",
        lambda _client: org_repo,
    )
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationMembershipRepository,
        "from_client",
        lambda _client: membership_repo,
    )
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)

    result = await surreal_organization_runtime.remove_org_member(
        slug="electric-coven",
        actor_id=actor_id,
        target_user_id=actor_id,
        request=SimpleNamespace(),
    )

    membership_repo.remove_member.assert_awaited_once_with(
        organization_id=org.id,
        user_id=actor_id,
    )
    assert result.org_id == org.id
    assert result.user_id == actor_id


@pytest.mark.asyncio
async def test_surreal_list_org_invitations_filters_accepted_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = SimpleNamespace(id=uuid4(), slug="electric-coven")

    class FakeClient:
        async def execute_query(self, query: str, **_params):
            assert "organization_invitations" in query
            return [
                {
                    "uuid": str(UUID("00000000-0000-0000-0000-000000000111")),
                    "organization_id": str(organization.id),
                    "invited_email": "ember@example.com",
                    "invited_role": OrganizationRole.ADMIN.value,
                    "token": "pending-token",
                    "created_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
                    "expires_at": datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
                    "accepted_at": None,
                },
                {
                    "uuid": str(UUID("00000000-0000-0000-0000-000000000222")),
                    "organization_id": str(organization.id),
                    "invited_email": "taken@example.com",
                    "invited_role": OrganizationRole.MEMBER.value,
                    "token": "accepted-token",
                    "created_at": datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
                    "expires_at": datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
                    "accepted_at": datetime(2026, 4, 19, 18, 0, tzinfo=UTC),
                },
            ]

        async def close(self) -> None:
            return None

    @asynccontextmanager
    async def fake_scope():
        yield FakeClient()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime,
        "_require_org_admin",
        AsyncMock(return_value=(organization, SimpleNamespace(role=OrganizationRole.ADMIN))),
    )

    result = await surreal_organization_runtime.list_org_invitations(
        slug="electric-coven",
        actor_id=uuid4(),
    )

    assert len(result) == 1
    assert result[0].email == "ember@example.com"
    assert result[0].role == OrganizationRole.ADMIN


@pytest.mark.asyncio
async def test_surreal_accept_org_invitation_creates_session_and_marks_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000111"),
        email="ember@example.com",
    )
    organization = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000333"),
        slug="electric-coven",
        name="Electric Coven",
    )
    refresh_expires = datetime.now(UTC) + timedelta(days=7)
    invite_created_at = datetime.now(UTC) - timedelta(days=1)
    invite_record = {
        "uuid": str(UUID("00000000-0000-0000-0000-000000000222")),
        "organization_id": str(organization.id),
        "invited_email": "ember@example.com",
        "invited_role": OrganizationRole.ADMIN.value,
        "token": "invite-token",
        "created_by_user_id": str(uuid4()),
        "expires_at": datetime.now(UTC) + timedelta(days=7),
        "accepted_at": None,
        "accepted_by_user_id": None,
        "created_at": invite_created_at,
        "updated_at": invite_created_at,
    }

    class FakeClient:
        def __init__(self) -> None:
            self.written_invitation: dict[str, object] | None = None

        async def execute_query(self, query: str, **params):
            if "SELECT * FROM organization_invitations WHERE token" in query:
                return [invite_record]
            if "DELETE FROM organization_invitations" in query:
                return []
            if "CREATE organization_invitations CONTENT $record" in query:
                self.written_invitation = params["record"]
                return [params["record"]]
            return []

        async def close(self) -> None:
            return None

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    membership_repo = SimpleNamespace(add_member=AsyncMock())
    org_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=organization))
    session_repo = SimpleNamespace(
        get_session_by_token=AsyncMock(return_value=None),
        rotate_tokens=AsyncMock(),
        create_session=AsyncMock(),
    )
    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationMembershipRepository,
        "from_client",
        lambda _client: membership_repo,
    )
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationRepository,
        "from_client",
        lambda _client: org_repo,
    )
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealSessionRepository,
        "from_client",
        lambda _client: session_repo,
    )
    monkeypatch.setattr(
        surreal_organization_runtime,
        "create_access_token",
        lambda **_kwargs: "access-token",
    )
    monkeypatch.setattr(
        surreal_organization_runtime,
        "create_refresh_token",
        lambda **_kwargs: ("refresh-token", refresh_expires),
    )
    monkeypatch.setattr(
        surreal_organization_runtime,
        "select_access_token",
        lambda **_kwargs: "current-token",
    )
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)

    result = await surreal_organization_runtime.accept_org_invitation(
        token="invite-token",
        user=user,
        request=_request(),
    )

    membership_repo.add_member.assert_awaited_once_with(
        organization_id=organization.id,
        user_id=user.id,
        role=OrganizationRole.ADMIN,
    )
    session_repo.create_session.assert_awaited_once()
    session_repo.rotate_tokens.assert_not_awaited()
    assert fake_client.written_invitation is not None
    assert fake_client.written_invitation["accepted_by_user_id"] == str(user.id)
    assert result.organization_id == organization.id
    assert result.invitation_id == UUID("00000000-0000-0000-0000-000000000222")


@pytest.mark.asyncio
async def test_surreal_list_org_members_batches_user_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid4()
    organization = SimpleNamespace(id=uuid4())
    user_a_id = uuid4()
    user_b_id = uuid4()
    created_at = datetime.now(UTC)

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "FROM users" in query:
                return [
                    {
                        "uuid": str(user_a_id),
                        "github_id": 123,
                        "email": "a@example.com",
                        "name": "A",
                        "avatar_url": "https://example.com/a.png",
                    },
                    {
                        "uuid": str(user_b_id),
                        "github_id": None,
                        "email": "b@example.com",
                        "name": "B",
                        "avatar_url": None,
                    },
                ]
            return []

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    org_repo = SimpleNamespace(get_by_slug=AsyncMock(return_value=organization))
    membership_repo = SimpleNamespace(
        get_for_user=AsyncMock(return_value=SimpleNamespace(role=OrganizationRole.OWNER)),
        list_for_org=AsyncMock(
            return_value=[
                SimpleNamespace(
                    user_id=user_a_id,
                    role=OrganizationRole.OWNER,
                    created_at=created_at,
                ),
                SimpleNamespace(
                    user_id=user_b_id,
                    role=OrganizationRole.MEMBER,
                    created_at=created_at,
                ),
            ]
        ),
    )

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationRepository,
        "from_client",
        lambda _client: org_repo,
    )
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealOrganizationMembershipRepository,
        "from_client",
        lambda _client: membership_repo,
    )

    rows = await surreal_organization_runtime.list_org_members(
        slug="team",
        actor_id=actor_id,
    )

    assert [row["user"]["email"] for row in rows] == ["a@example.com", "b@example.com"]
    assert len(fake_client.calls) == 1
    assert "FROM users" in fake_client.calls[0][0]
    assert fake_client.calls[0][1] == {"user_ids": [str(user_a_id), str(user_b_id)]}


@pytest.mark.asyncio
async def test_surreal_list_project_members_batches_user_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=uuid4())
    org_id = uuid4()
    owner_id = uuid4()
    member_id = uuid4()
    project = SimpleNamespace(
        id=uuid4(),
        owner_user_id=owner_id,
        created_at=datetime.now(UTC),
    )
    member_created_at = datetime.now(UTC)

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "FROM project_members" in query:
                return [
                    {
                        "uuid": str(uuid4()),
                        "project_id": str(project.id),
                        "user_id": str(owner_id),
                        "role": ProjectRole.MAINTAINER.value,
                        "created_at": member_created_at,
                    },
                    {
                        "uuid": str(uuid4()),
                        "project_id": str(project.id),
                        "user_id": str(member_id),
                        "role": ProjectRole.CONTRIBUTOR.value,
                        "created_at": member_created_at,
                    },
                    {
                        "uuid": str(uuid4()),
                        "project_id": str(project.id),
                        "user_id": str(member_id),
                        "role": ProjectRole.VIEWER.value,
                        "created_at": member_created_at,
                    },
                ]
            if "FROM users" in query:
                return [
                    {
                        "uuid": str(owner_id),
                        "email": "owner@example.com",
                        "name": "Owner",
                        "avatar_url": None,
                    },
                    {
                        "uuid": str(member_id),
                        "email": "member@example.com",
                        "name": "Member",
                        "avatar_url": None,
                    },
                ]
            return []

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime,
        "_get_project_and_user_role",
        AsyncMock(return_value=(project, ProjectRole.OWNER)),
    )

    result = await surreal_organization_runtime.list_project_members(
        project_id="project_123",
        actor=actor,
        org_id=org_id,
    )

    assert [row["user"]["email"] for row in result.members] == [
        "owner@example.com",
        "member@example.com",
    ]
    assert result.members[0]["is_owner"] is True
    assert result.members[1]["role"] == ProjectRole.CONTRIBUTOR.value
    assert len(fake_client.calls) == 2
    assert "FROM project_members" in fake_client.calls[0][0]
    assert "FROM users" in fake_client.calls[1][0]
    assert fake_client.calls[1][1] == {"user_ids": [str(owner_id), str(member_id)]}


@pytest.mark.asyncio
async def test_surreal_delete_project_member_records_batches_deletes() -> None:
    membership_a = uuid4()
    membership_b = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            return []

    fake_client = FakeClient()

    await surreal_organization_runtime._delete_project_member_records(
        fake_client,
        membership_records=[
            {"uuid": str(membership_a)},
            {"uuid": str(membership_b)},
        ],
    )

    assert len(fake_client.calls) == 1
    assert fake_client.calls[0][0] == "DELETE FROM project_members WHERE uuid IN $membership_ids;"
    assert fake_client.calls[0][1] == {
        "membership_ids": [str(membership_a), str(membership_b)]
    }


@pytest.mark.asyncio
async def test_surreal_add_project_member_rejects_existing_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=uuid4())
    org_id = uuid4()
    target_user_id = uuid4()
    project = SimpleNamespace(id=uuid4(), owner_user_id=actor.id)

    class FakeClient:
        async def execute_query(self, query: str, **_params):
            if "SELECT * FROM project_members" in query:
                return [{"uuid": str(uuid4())}]
            return []

        async def close(self) -> None:
            return None

    @asynccontextmanager
    async def fake_scope():
        yield FakeClient()

    user_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(id=target_user_id))
    )

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime,
        "_get_project_and_user_role",
        AsyncMock(return_value=(project, ProjectRole.OWNER)),
    )
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealUserRepository,
        "from_client",
        lambda _client: user_repo,
    )

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.add_project_member(
            request=SimpleNamespace(),
            project_id="project_123",
            actor=actor,
            org_id=org_id,
            target_user_id=target_user_id,
            role=ProjectRole.CONTRIBUTOR,
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_surreal_add_project_member_handles_unique_index_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=uuid4())
    org_id = uuid4()
    target_user_id = uuid4()
    project = SimpleNamespace(id=uuid4(), owner_user_id=actor.id)

    class FakeClient:
        async def execute_query(self, query: str, **_params):
            if "SELECT * FROM project_members" in query:
                return []
            if "CREATE project_members CONTENT $record" in query:
                return {
                    "status": "ERR",
                    "detail": "Database index `idx_project_members_project_user` already contains",
                }
            return []

        async def close(self) -> None:
            return None

    @asynccontextmanager
    async def fake_scope():
        yield FakeClient()

    user_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(id=target_user_id))
    )

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime,
        "_get_project_and_user_role",
        AsyncMock(return_value=(project, ProjectRole.OWNER)),
    )
    monkeypatch.setattr(
        surreal_organization_runtime.SurrealUserRepository,
        "from_client",
        lambda _client: user_repo,
    )

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.add_project_member(
            request=_request(),
            project_id="project_123",
            actor=actor,
            org_id=org_id,
            target_user_id=target_user_id,
            role=ProjectRole.CONTRIBUTOR,
        )

    assert exc_info.value.status_code == 409
