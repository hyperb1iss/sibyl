from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from sibyl.persistence import organization_common, organization_runtime
from sibyl.persistence.surreal import organization_runtime as surreal_organization_runtime
from sibyl_core.auth import OrganizationRole, ProjectRole


def _request(*, authorization: str | None = "Bearer current-token") -> SimpleNamespace:
    headers: dict[str, str] = {}
    if authorization is not None:
        headers["authorization"] = authorization
    return SimpleNamespace(
        headers=headers,
        cookies={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def test_organization_runtime_exports_neutral_runtime_surface() -> None:
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
    assert "list_orgs" in organization_runtime.__all__
    assert "list_org_ids" in organization_runtime.__all__
    assert hasattr(surreal_organization_runtime, "list_project_members")


def test_organization_runtime_uses_surreal_when_postgres_auth_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(organization_runtime.settings, "auth_store", "postgres")

    assert (
        organization_runtime._resolve_backend_export("list_orgs")
        is surreal_organization_runtime.list_orgs
    )
    assert (
        organization_runtime._resolve_backend_export("list_project_members")
        is surreal_organization_runtime.list_project_members
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
async def test_surreal_delete_org_batches_authorization_and_deletes_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    user_id = uuid4()
    source_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {
                        "uuid": str(org_id),
                        "slug": "electric-coven",
                        "name": "Electric Coven",
                        "is_personal": False,
                    },
                    "membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(user_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                }
            return []

    class FakeContentClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "SELECT * FROM crawl_sources" in query:
                return [{"uuid": str(source_id)}]
            return []

    fake_client = FakeClient()
    fake_content_client = FakeContentClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    @asynccontextmanager
    async def fake_content_scope():
        yield fake_content_client

    delete_auth_children = AsyncMock()
    delete_crawl_source = AsyncMock()
    delete_graph = AsyncMock()
    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime,
        "_delete_org_auth_child_records",
        delete_auth_children,
    )
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)
    monkeypatch.setattr(surreal_organization_runtime, "delete_graph_data", delete_graph)
    monkeypatch.setattr(
        "sibyl.persistence.surreal.content.surreal_content_client",
        fake_content_scope,
    )
    monkeypatch.setattr(
        "sibyl.persistence.surreal.content.delete_crawl_source_record",
        delete_crawl_source,
    )

    await surreal_organization_runtime.delete_org(
        request=_request(),
        slug="electric-coven",
        user_id=user_id,
    )

    lookup_query, lookup_params = fake_client.calls[0]
    assert "RETURN" in lookup_query
    assert "FROM organizations" in lookup_query
    assert "FROM organization_members" in lookup_query
    assert lookup_params == {"slug": "electric-coven", "user_id": str(user_id)}
    assert fake_client.calls[-1] == (
        "DELETE FROM organizations WHERE uuid = $organization_id;",
        {"organization_id": str(org_id)},
    )
    delete_auth_children.assert_awaited_once_with(fake_client, organization_id=org_id)
    delete_crawl_source.assert_awaited_once_with(
        None,
        source_id=source_id,
        organization_id=org_id,
    )
    delete_graph.assert_awaited_once_with(str(org_id))
    audit_log.assert_awaited_once()


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
            if "RETURN" in query:
                return {
                    "memberships": [
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
                    ],
                    "organizations": [
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
                    ],
                }
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
    assert len(fake_client.calls) == 1
    assert "FROM organization_members" in fake_client.calls[0][0]
    assert fake_client.calls[0][1] == {"user_id": str(user_id)}
    assert "FROM organizations" in fake_client.calls[0][0]


@pytest.mark.asyncio
async def test_surreal_get_org_batches_org_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {
                        "uuid": str(org_id),
                        "slug": "electric-coven",
                        "name": "Electric Coven",
                    },
                    "membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(user_id),
                        "role": OrganizationRole.ADMIN.value,
                    },
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    result = await surreal_organization_runtime.get_org(
        slug="electric-coven",
        user_id=user_id,
    )

    assert result.id == org_id
    assert result.slug == "electric-coven"
    assert result.name == "Electric Coven"
    assert result.role is OrganizationRole.ADMIN
    assert len(fake_client.calls) == 1
    query, params = fake_client.calls[0]
    assert "RETURN" in query
    assert "FROM organizations" in query
    assert "FROM organization_members" in query
    assert params == {"slug": "electric-coven", "user_id": str(user_id)}


@pytest.mark.asyncio
async def test_surreal_switch_org_batches_org_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    refresh_expires = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {
                        "uuid": str(org_id),
                        "slug": "electric-coven",
                        "name": "Electric Coven",
                    },
                    "membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(user_id),
                        "role": OrganizationRole.MEMBER.value,
                    },
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    session_repo = SimpleNamespace(
        get_session_by_token=AsyncMock(return_value=None),
        rotate_tokens=AsyncMock(),
        create_session=AsyncMock(),
    )
    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
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

    result = await surreal_organization_runtime.switch_org(
        request=_request(),
        slug="electric-coven",
        user_id=user_id,
    )

    assert result.id == org_id
    assert result.access_token == "access-token"
    assert len(fake_client.calls) == 1
    assert "RETURN" in fake_client.calls[0][0]
    session_repo.create_session.assert_awaited_once()
    session_repo.rotate_tokens.assert_not_awaited()
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_surreal_require_org_admin_batches_org_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {
                        "uuid": str(org_id),
                        "slug": "electric-coven",
                        "name": "Electric Coven",
                    },
                    "membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(user_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    organization, membership = await surreal_organization_runtime._require_org_admin(
        slug="electric-coven",
        user_id=user_id,
    )

    assert organization.id == org_id
    assert organization.slug == "electric-coven"
    assert membership.id == membership_id
    assert membership.role is OrganizationRole.OWNER
    assert len(fake_client.calls) == 1
    query, params = fake_client.calls[0]
    assert "RETURN" in query
    assert "FROM organizations" in query
    assert "FROM organization_members" in query
    assert params == {"slug": "electric-coven", "user_id": str(user_id)}


@pytest.mark.asyncio
async def test_surreal_list_org_ids_reads_surreal_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_ids = [
        UUID("00000000-0000-0000-0000-000000000111"),
        UUID("00000000-0000-0000-0000-000000000222"),
    ]

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            return [{"uuid": str(org_id)} for org_id in org_ids]

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    result = await surreal_organization_runtime.list_org_ids()

    assert result == [str(org_id) for org_id in org_ids]
    assert len(fake_client.calls) == 1
    assert "SELECT uuid, created_at FROM organizations" in fake_client.calls[0][0]
    assert fake_client.calls[0][1] == {"limit": 100_000}


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
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "SELECT uuid FROM organizations" in query:
                return []
            if "CREATE organizations CONTENT $record" in query:
                return [
                    {
                        "uuid": str(organization.id),
                        "slug": organization.slug,
                        "name": organization.name,
                        "is_personal": False,
                    }
                ]
            if "CREATE organization_members CONTENT $record" in query:
                return [params["record"]]
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    session_repo = SimpleNamespace(
        get_session_by_token=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
        rotate_tokens=AsyncMock(),
        create_session=AsyncMock(),
    )
    ensure_indexes = AsyncMock()
    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
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

    assert len(fake_client.calls) == 3
    assert "SELECT uuid FROM organizations" in fake_client.calls[0][0]
    assert "CREATE organizations CONTENT $record" in fake_client.calls[1][0]
    assert "CREATE organization_members CONTENT $record" in fake_client.calls[2][0]
    membership_record = fake_client.calls[2][1]["record"]
    assert membership_record["organization_id"] == str(organization.id)
    assert membership_record["user_id"] == str(user_id)
    assert membership_record["role"] == OrganizationRole.OWNER.value
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
    org_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {
                        "uuid": str(org_id),
                        "slug": "old-name",
                        "name": "Old Name",
                        "is_personal": False,
                    },
                    "membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(user_id),
                        "role": OrganizationRole.ADMIN.value,
                    },
                }
            if "SELECT * FROM organizations WHERE slug" in query:
                return []
            if query.startswith("UPDATE organizations"):
                return [
                    {
                        "uuid": str(org_id),
                        "slug": "new-name",
                        "name": "New Name",
                        "is_personal": False,
                    }
                ]
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)

    result = await surreal_organization_runtime.update_org(
        request=_request(),
        slug="old-name",
        user_id=user_id,
        name="New Name",
        new_slug="new-name",
    )

    assert result.id == org_id
    assert result.slug == "new-name"
    assert result.name == "New Name"
    assert result.role == OrganizationRole.ADMIN
    assert len(fake_client.calls) == 3
    lookup_query, lookup_params = fake_client.calls[0]
    assert "RETURN" in lookup_query
    assert "FROM organizations" in lookup_query
    assert "FROM organization_members" in lookup_query
    assert lookup_params == {"slug": "old-name", "user_id": str(user_id)}
    conflict_query, conflict_params = fake_client.calls[1]
    assert "SELECT * FROM organizations WHERE slug" in conflict_query
    assert conflict_params == {"slug": "new-name"}
    update_query, update_params = fake_client.calls[2]
    assert update_query.startswith("UPDATE organizations")
    assert update_params["uuid"] == str(org_id)
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_surreal_add_org_member_batches_lookup_and_updates_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    target_user_id = uuid4()
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.ADMIN.value,
                    },
                    "target_user": {"uuid": str(target_user_id)},
                    "target_membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(target_user_id),
                        "role": OrganizationRole.VIEWER.value,
                    },
                }
            if "UPDATE organization_members" in query:
                return [
                    {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(target_user_id),
                        "role": params["role"],
                    }
                ]
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)
    result = await surreal_organization_runtime.add_org_member(
        slug="electric-coven",
        actor_id=actor_id,
        target_user_id=target_user_id,
        role=OrganizationRole.MEMBER,
        request=_request(),
    )

    assert result.org_id == org_id
    assert result.user_id == target_user_id
    assert result.role is OrganizationRole.MEMBER
    assert len(fake_client.calls) == 2
    lookup_query, lookup_params = fake_client.calls[0]
    assert "RETURN" in lookup_query
    assert "FROM organizations" in lookup_query
    assert "FROM organization_members" in lookup_query
    assert "FROM users" in lookup_query
    assert lookup_params == {
        "slug": "electric-coven",
        "actor_id": str(actor_id),
        "target_user_id": str(target_user_id),
        "owner_role": OrganizationRole.OWNER.value,
    }
    write_query, write_params = fake_client.calls[1]
    assert "UPDATE organization_members" in write_query
    assert "CREATE organization_members" not in write_query
    assert write_params["uuid"] == str(membership_id)
    assert write_params["role"] == OrganizationRole.MEMBER.value
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_surreal_add_org_member_batches_lookup_and_creates_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    target_user_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                    "target_user": {"uuid": str(target_user_id)},
                    "target_membership": None,
                }
            if "CREATE organization_members CONTENT $record" in query:
                return [params["record"]]
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)

    result = await surreal_organization_runtime.add_org_member(
        slug="electric-coven",
        actor_id=actor_id,
        target_user_id=target_user_id,
        role=OrganizationRole.VIEWER,
        request=_request(),
    )

    assert result.org_id == org_id
    assert result.user_id == target_user_id
    assert result.role is OrganizationRole.VIEWER
    assert len(fake_client.calls) == 2
    assert "RETURN" in fake_client.calls[0][0]
    assert "CREATE organization_members CONTENT $record" in fake_client.calls[1][0]
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_surreal_add_org_member_rejects_admin_granting_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    target_user_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.ADMIN.value,
                    },
                    "target_user": {"uuid": str(target_user_id)},
                    "target_membership": None,
                    "owner_memberships": [{"uuid": str(uuid4())}],
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.add_org_member(
            slug="electric-coven",
            actor_id=actor_id,
            target_user_id=target_user_id,
            role=OrganizationRole.OWNER,
            request=_request(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Only organization owners can manage owner roles"
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_surreal_add_org_member_rejects_last_owner_demotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    target_user_id = uuid4()
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                    "target_user": {"uuid": str(target_user_id)},
                    "target_membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(target_user_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                    "owner_memberships": [{"uuid": str(membership_id)}],
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.add_org_member(
            slug="electric-coven",
            actor_id=actor_id,
            target_user_id=target_user_id,
            role=OrganizationRole.ADMIN,
            request=_request(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Cannot demote the last organization owner"
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_surreal_update_org_member_role_batches_lookup_and_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    target_user_id = uuid4()
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.ADMIN.value,
                    },
                    "target_membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(target_user_id),
                        "role": OrganizationRole.MEMBER.value,
                    },
                    "owner_memberships": [{"uuid": str(uuid4())}],
                }
            if "UPDATE organization_members" in query:
                return [
                    {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(target_user_id),
                        "role": params["role"],
                    }
                ]
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)

    result = await surreal_organization_runtime.update_org_member_role(
        slug="electric-coven",
        actor_id=actor_id,
        target_user_id=target_user_id,
        role=OrganizationRole.VIEWER,
        request=_request(),
    )

    assert result.org_id == org_id
    assert result.user_id == target_user_id
    assert result.role is OrganizationRole.VIEWER
    assert len(fake_client.calls) == 2
    assert "RETURN" in fake_client.calls[0][0]
    assert "UPDATE organization_members" in fake_client.calls[1][0]
    assert fake_client.calls[1][1]["uuid"] == str(membership_id)
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_surreal_update_org_member_role_rejects_last_owner_demotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    target_user_id = uuid4()
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                    "target_membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(target_user_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                    "owner_memberships": [{"uuid": str(membership_id)}],
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.update_org_member_role(
            slug="electric-coven",
            actor_id=actor_id,
            target_user_id=target_user_id,
            role=OrganizationRole.ADMIN,
            request=_request(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Cannot demote the last organization owner"
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_surreal_update_org_member_role_rejects_admin_owner_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    target_user_id = uuid4()
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.ADMIN.value,
                    },
                    "target_membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(target_user_id),
                        "role": OrganizationRole.MEMBER.value,
                    },
                    "owner_memberships": [{"uuid": str(uuid4())}],
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.update_org_member_role(
            slug="electric-coven",
            actor_id=actor_id,
            target_user_id=target_user_id,
            role=OrganizationRole.OWNER,
            request=_request(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Only organization owners can manage owner roles"
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_surreal_update_org_member_role_rejects_admin_owner_demotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    target_user_id = uuid4()
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.ADMIN.value,
                    },
                    "target_membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(target_user_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                    "owner_memberships": [{"uuid": str(membership_id)}, {"uuid": str(uuid4())}],
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.update_org_member_role(
            slug="electric-coven",
            actor_id=actor_id,
            target_user_id=target_user_id,
            role=OrganizationRole.ADMIN,
            request=_request(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Only organization owners can manage owner roles"
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_surreal_remove_org_member_batches_lookup_and_allows_self_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.VIEWER.value,
                    },
                    "target_membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.VIEWER.value,
                    },
                    "owner_memberships": [{"uuid": str(uuid4())}],
                }
            if "DELETE FROM organization_members" in query:
                return []
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit_log)

    result = await surreal_organization_runtime.remove_org_member(
        slug="electric-coven",
        actor_id=actor_id,
        target_user_id=actor_id,
        request=SimpleNamespace(),
    )

    assert result.org_id == org_id
    assert result.user_id == actor_id
    assert len(fake_client.calls) == 2
    assert "RETURN" in fake_client.calls[0][0]
    assert "DELETE FROM organization_members" in fake_client.calls[1][0]
    assert fake_client.calls[1][1]["uuid"] == str(membership_id)
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_surreal_remove_org_member_rejects_admin_removing_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    actor_id = uuid4()
    target_user_id = uuid4()
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "electric-coven"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.ADMIN.value,
                    },
                    "target_membership": {
                        "uuid": str(membership_id),
                        "organization_id": str(org_id),
                        "user_id": str(target_user_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                    "owner_memberships": [{"uuid": str(membership_id)}, {"uuid": str(uuid4())}],
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.remove_org_member(
            slug="electric-coven",
            actor_id=actor_id,
            target_user_id=target_user_id,
            request=_request(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Only organization owners can manage owner roles"
    assert len(fake_client.calls) == 1


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
async def test_surreal_create_org_invitation_rejects_admin_owner_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = SimpleNamespace(id=uuid4(), slug="electric-coven")

    monkeypatch.setattr(
        surreal_organization_runtime,
        "_require_org_admin",
        AsyncMock(return_value=(organization, SimpleNamespace(role=OrganizationRole.ADMIN))),
    )

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.create_org_invitation(
            slug="electric-coven",
            actor_id=uuid4(),
            email="ember@example.com",
            role=OrganizationRole.OWNER,
            expires_days=7,
            request=_request(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Only organization owners can manage owner roles"


@pytest.mark.asyncio
async def test_surreal_accept_org_invitation_rejects_admin_created_owner_invite(
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
    invite_record = {
        "uuid": str(UUID("00000000-0000-0000-0000-000000000222")),
        "organization_id": str(organization.id),
        "invited_email": "ember@example.com",
        "invited_role": OrganizationRole.OWNER.value,
        "token": "invite-token",
        "created_by_user_id": str(uuid4()),
        "expires_at": datetime.now(UTC) + timedelta(days=7),
        "accepted_at": None,
        "accepted_by_user_id": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "invitation": invite_record,
                    "organization": {
                        "uuid": str(organization.id),
                        "slug": organization.slug,
                        "name": organization.name,
                    },
                    "membership": None,
                    "creator_membership": {"role": OrganizationRole.ADMIN.value},
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.accept_org_invitation(
            token="invite-token",
            user=user,
            request=_request(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Only organization owners can manage owner roles"
    assert len(fake_client.calls) == 1


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
            self.written_membership: dict[str, object] | None = None
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "invitation": invite_record,
                    "organization": {
                        "uuid": str(organization.id),
                        "slug": organization.slug,
                        "name": organization.name,
                    },
                    "membership": None,
                }
            if "CREATE organization_members CONTENT $record" in query:
                self.written_membership = params["record"]
                return [params["record"]]
            if "UPSERT organization_invitations CONTENT $record" in query:
                self.written_invitation = params["record"]
                return [params["record"]]
            raise AssertionError(query)

        async def close(self) -> None:
            return None

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    session_repo = SimpleNamespace(
        get_session_by_token=AsyncMock(return_value=None),
        rotate_tokens=AsyncMock(),
        create_session=AsyncMock(),
    )
    audit_log = AsyncMock()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
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

    assert fake_client.written_membership is not None
    assert fake_client.written_membership["organization_id"] == str(organization.id)
    assert fake_client.written_membership["user_id"] == str(user.id)
    assert fake_client.written_membership["role"] == OrganizationRole.ADMIN.value
    session_repo.create_session.assert_awaited_once()
    session_repo.rotate_tokens.assert_not_awaited()
    assert fake_client.written_invitation is not None
    assert fake_client.written_invitation["accepted_by_user_id"] == str(user.id)
    queries = [query for query, _params in fake_client.calls]
    assert "RETURN" in queries[0]
    assert any("CREATE organization_members CONTENT $record" in query for query in queries)
    assert any("UPSERT organization_invitations CONTENT $record" in query for query in queries)
    assert all("DELETE FROM organization_invitations" not in query for query in queries)
    assert result.organization_id == organization.id
    assert result.invitation_id == UUID("00000000-0000-0000-0000-000000000222")


@pytest.mark.asyncio
async def test_surreal_list_org_members_batches_user_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid4()
    org_id = uuid4()
    user_a_id = uuid4()
    user_b_id = uuid4()
    created_at = datetime.now(UTC)

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "organization": {"uuid": str(org_id), "slug": "team"},
                    "actor_membership": {
                        "uuid": str(uuid4()),
                        "organization_id": str(org_id),
                        "user_id": str(actor_id),
                        "role": OrganizationRole.OWNER.value,
                    },
                    "memberships": [
                        {
                            "uuid": str(uuid4()),
                            "organization_id": str(org_id),
                            "user_id": str(user_a_id),
                            "role": OrganizationRole.OWNER.value,
                            "created_at": created_at,
                        },
                        {
                            "uuid": str(uuid4()),
                            "organization_id": str(org_id),
                            "user_id": str(user_b_id),
                            "role": OrganizationRole.MEMBER.value,
                            "created_at": created_at,
                        },
                    ],
                    "users": [
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
                    ],
                }
            raise AssertionError(query)

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_scope():
        yield fake_client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)

    rows = await surreal_organization_runtime.list_org_members(
        slug="team",
        actor_id=actor_id,
    )

    assert [row["user"]["email"] for row in rows] == ["a@example.com", "b@example.com"]
    assert len(fake_client.calls) == 1
    query, params = fake_client.calls[0]
    assert "RETURN" in query
    assert "FROM organizations" in query
    assert "FROM organization_members" in query
    assert "FROM users" in query
    assert params == {"slug": "team", "actor_id": str(actor_id)}


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
            if "RETURN" in query:
                return {
                    "members": [
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
                    ],
                    "users": [
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
                    ],
                    "org_members": [
                        {"user_id": str(owner_id)},
                        {"user_id": str(member_id)},
                    ],
                }
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
    assert len(fake_client.calls) == 1
    assert "FROM project_members" in fake_client.calls[0][0]
    assert "FROM users" in fake_client.calls[0][0]
    assert fake_client.calls[0][1] == {
        "project_id": str(project.id),
        "owner_user_id": str(owner_id),
        "organization_id": str(org_id),
    }


@pytest.mark.asyncio
async def test_surreal_project_role_lookup_batches_project_and_membership() -> None:
    org_id = uuid4()
    user_id = uuid4()
    owner_id = uuid4()
    project_db_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            return {
                "project": {
                    "uuid": str(project_db_id),
                    "organization_id": str(org_id),
                    "graph_project_id": "project_123",
                    "owner_user_id": str(owner_id),
                },
                "membership": {
                    "uuid": str(uuid4()),
                    "project_id": str(project_db_id),
                    "user_id": str(user_id),
                    "role": ProjectRole.MAINTAINER.value,
                },
                "org_membership": {
                    "uuid": str(uuid4()),
                    "organization_id": str(org_id),
                    "user_id": str(user_id),
                    "role": OrganizationRole.MEMBER.value,
                },
            }

    fake_client = FakeClient()

    project, role = await surreal_organization_runtime._get_project_and_user_role(
        client=fake_client,
        project_id="project_123",
        user_id=user_id,
        org_id=org_id,
    )

    assert project.id == project_db_id
    assert role is ProjectRole.MAINTAINER
    assert len(fake_client.calls) == 1
    query, params = fake_client.calls[0]
    assert "RETURN" in query
    assert "FROM projects" in query
    assert "FROM project_members" in query
    assert params == {
        "organization_id": str(org_id),
        "user_id": str(user_id),
        "graph_project_id": "project_123",
    }


@pytest.mark.asyncio
async def test_surreal_project_role_lookup_requires_org_membership() -> None:
    org_id = uuid4()
    user_id = uuid4()
    project_db_id = uuid4()

    class FakeClient:
        async def execute_query(self, _query: str, **_params):
            return {
                "project": {
                    "uuid": str(project_db_id),
                    "organization_id": str(org_id),
                    "graph_project_id": "project_123",
                    "owner_user_id": str(uuid4()),
                },
                "membership": {
                    "uuid": str(uuid4()),
                    "project_id": str(project_db_id),
                    "user_id": str(user_id),
                    "role": ProjectRole.MAINTAINER.value,
                },
                "org_membership": None,
            }

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime._get_project_and_user_role(
            client=FakeClient(),
            project_id="project_123",
            user_id=user_id,
            org_id=org_id,
        )

    assert exc_info.value.status_code == 403


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
    assert fake_client.calls[0][1] == {"membership_ids": [str(membership_a), str(membership_b)]}


@pytest.mark.asyncio
async def test_surreal_update_project_member_role_uses_batch_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=uuid4())
    org_id = uuid4()
    target_user_id = uuid4()
    project = SimpleNamespace(id=uuid4(), owner_user_id=uuid4())
    membership_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "SELECT * FROM organization_members" in query:
                return [{"user_id": str(target_user_id)}]
            if "SELECT * FROM project_members" in query:
                return [
                    {
                        "uuid": str(membership_id),
                        "project_id": str(project.id),
                        "user_id": str(target_user_id),
                        "role": ProjectRole.CONTRIBUTOR.value,
                    }
                ]
            if "UPDATE project_members SET role" in query:
                return [
                    {
                        "uuid": str(membership_id),
                        "project_id": str(project.id),
                        "user_id": str(target_user_id),
                        "role": ProjectRole.MAINTAINER.value,
                    }
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
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", AsyncMock())

    result = await surreal_organization_runtime.update_project_member_role(
        request=_request(),
        project_id="project_123",
        actor=actor,
        org_id=org_id,
        target_user_id=target_user_id,
        role=ProjectRole.MAINTAINER,
    )

    assert result.role is ProjectRole.MAINTAINER
    queries = [query for query, _params in fake_client.calls]
    assert any("UPDATE project_members SET role" in query for query in queries)
    assert all("DELETE FROM project_members" not in query for query in queries)
    assert all("CREATE project_members CONTENT $record" not in query for query in queries)


@pytest.mark.asyncio
async def test_surreal_update_project_member_role_rejects_non_org_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=uuid4())
    org_id = uuid4()
    target_user_id = uuid4()
    project = SimpleNamespace(id=uuid4(), owner_user_id=uuid4())

    class FakeClient:
        async def execute_query(self, query: str, **_params):
            if "SELECT * FROM organization_members" in query:
                return []
            raise AssertionError(query)

    @asynccontextmanager
    async def fake_scope():
        yield FakeClient()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime,
        "_get_project_and_user_role",
        AsyncMock(return_value=(project, ProjectRole.OWNER)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.update_project_member_role(
            request=_request(),
            project_id="project_123",
            actor=actor,
            org_id=org_id,
            target_user_id=target_user_id,
            role=ProjectRole.MAINTAINER,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "User is not an organization member"


@pytest.mark.asyncio
async def test_surreal_add_project_member_rejects_existing_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=uuid4())
    org_id = uuid4()
    target_user_id = uuid4()
    project = SimpleNamespace(id=uuid4(), owner_user_id=actor.id)

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params):
            self.calls.append((query, params))
            if "RETURN" in query:
                return {
                    "user": {"uuid": str(target_user_id)},
                    "org_membership": {"user_id": str(target_user_id)},
                    "membership": {"uuid": str(uuid4())},
                }
            return []

        async def close(self) -> None:
            return None

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
    assert len(fake_client.calls) == 1
    query, params = fake_client.calls[0]
    assert "RETURN" in query
    assert "FROM users" in query
    assert "FROM organization_members" in query
    assert "FROM project_members" in query
    assert params == {
        "project_id": str(project.id),
        "user_id": str(target_user_id),
        "organization_id": str(org_id),
    }


@pytest.mark.asyncio
async def test_surreal_add_project_member_rejects_non_org_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=uuid4())
    org_id = uuid4()
    target_user_id = uuid4()
    project = SimpleNamespace(id=uuid4(), owner_user_id=actor.id)

    class FakeClient:
        async def execute_query(self, query: str, **_params):
            if "RETURN" in query:
                return {
                    "user": {"uuid": str(target_user_id)},
                    "org_membership": None,
                    "membership": None,
                }
            return []

    @asynccontextmanager
    async def fake_scope():
        yield FakeClient()

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime,
        "_get_project_and_user_role",
        AsyncMock(return_value=(project, ProjectRole.OWNER)),
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

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "User is not an organization member"


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
            if "RETURN" in query:
                return {
                    "user": {"uuid": str(target_user_id)},
                    "org_membership": {"user_id": str(target_user_id)},
                    "membership": None,
                }
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

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime,
        "_get_project_and_user_role",
        AsyncMock(return_value=(project, ProjectRole.OWNER)),
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
