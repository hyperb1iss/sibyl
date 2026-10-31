"""A project's default write scope: settings authority and capture resolution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes import memory_auth
from sibyl.api.schemas import RawMemoryRememberRequest
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.mcp_tools import context as mcp_context_module, policy as mcp_policy
from sibyl.mcp_tools.context import McpContext
from sibyl.persistence.surreal import organization_runtime as surreal_organization_runtime
from sibyl_core.auth import OrganizationRole, ProjectRole, ProjectVisibility
from sibyl_core.models.memory_scope import MemoryScope


def _request() -> SimpleNamespace:
    return SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="127.0.0.1"))


def _project(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid4(),
        "owner_user_id": uuid4(),
        "graph_project_id": "project_oncall",
        "visibility": ProjectVisibility.ORG.value,
        "default_memory_scope": "private",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeAuthClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[object]:
        self.calls.append((query, params))
        return []


def _patch_runtime(monkeypatch, *, project, role) -> _FakeAuthClient:
    client = _FakeAuthClient()

    @asynccontextmanager
    async def fake_scope():
        yield client

    monkeypatch.setattr(surreal_organization_runtime, "_auth_client_scope", fake_scope)
    monkeypatch.setattr(
        surreal_organization_runtime,
        "_get_project_and_user_role",
        AsyncMock(return_value=(project, role)),
    )
    return client


# --- settings authority ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_role", "org_role"),
    [
        (ProjectRole.MAINTAINER, OrganizationRole.MEMBER),
        (ProjectRole.OWNER, OrganizationRole.MEMBER),
        (None, OrganizationRole.ADMIN),
        (None, OrganizationRole.OWNER),
    ],
)
async def test_project_managers_and_org_admins_can_share_a_project(
    monkeypatch, project_role, org_role
) -> None:
    project = _project()
    client = _patch_runtime(monkeypatch, project=project, role=project_role)
    audit = AsyncMock()
    monkeypatch.setattr(surreal_organization_runtime, "log_audit_event", audit)
    actor = SimpleNamespace(id=uuid4())

    settings = await surreal_organization_runtime.update_project_memory_settings(
        request=_request(),
        project_id="project_oncall",
        actor=actor,
        org_id=uuid4(),
        org_role=org_role,
        default_memory_scope=MemoryScope.PROJECT,
    )

    assert settings.default_memory_scope is MemoryScope.PROJECT
    assert settings.can_manage is True
    (query, params), *_ = client.calls
    assert query.startswith("UPDATE projects SET default_memory_scope")
    assert params["default_memory_scope"] == "project"
    assert params["uuid"] == str(project.id)
    assert audit.await_args.kwargs["action"] == "project.settings.update"
    assert audit.await_args.kwargs["details"]["previous_default_memory_scope"] == "private"


@pytest.mark.asyncio
@pytest.mark.parametrize("project_role", [ProjectRole.CONTRIBUTOR, ProjectRole.VIEWER, None])
async def test_contributors_and_viewers_cannot_change_the_default(
    monkeypatch, project_role
) -> None:
    client = _patch_runtime(monkeypatch, project=_project(), role=project_role)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.update_project_memory_settings(
            request=_request(),
            project_id="project_oncall",
            actor=SimpleNamespace(id=uuid4()),
            org_id=uuid4(),
            org_role=OrganizationRole.MEMBER,
            default_memory_scope=MemoryScope.PROJECT,
        )

    assert exc_info.value.status_code == 403
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [MemoryScope.TEAM, MemoryScope.ORGANIZATION])
async def test_only_private_and_project_can_be_a_default(monkeypatch, scope) -> None:
    client = _patch_runtime(monkeypatch, project=_project(), role=ProjectRole.OWNER)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.update_project_memory_settings(
            request=_request(),
            project_id="project_oncall",
            actor=SimpleNamespace(id=uuid4()),
            org_id=uuid4(),
            org_role=OrganizationRole.MEMBER,
            default_memory_scope=scope,
        )

    assert exc_info.value.status_code == 400
    assert client.calls == []


@pytest.mark.asyncio
async def test_org_readers_see_the_default_without_managing_it(monkeypatch) -> None:
    _patch_runtime(monkeypatch, project=_project(default_memory_scope="project"), role=None)

    settings = await surreal_organization_runtime.get_project_memory_settings(
        project_id="project_oncall",
        actor=SimpleNamespace(id=uuid4()),
        org_id=uuid4(),
        org_role=OrganizationRole.MEMBER,
    )

    assert settings.default_memory_scope is MemoryScope.PROJECT
    assert settings.can_manage is False


@pytest.mark.asyncio
async def test_a_private_project_reads_as_absent_to_outsiders(monkeypatch) -> None:
    _patch_runtime(
        monkeypatch, project=_project(visibility=ProjectVisibility.PRIVATE.value), role=None
    )

    with pytest.raises(HTTPException) as exc_info:
        await surreal_organization_runtime.get_project_memory_settings(
            project_id="project_oncall",
            actor=SimpleNamespace(id=uuid4()),
            org_id=uuid4(),
            org_role=OrganizationRole.MEMBER,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_an_unknown_stored_default_reads_as_private(monkeypatch) -> None:
    _patch_runtime(monkeypatch, project=_project(default_memory_scope="team"), role=None)

    settings = await surreal_organization_runtime.get_project_memory_settings(
        project_id="project_oncall",
        actor=SimpleNamespace(id=uuid4()),
        org_id=uuid4(),
        org_role=OrganizationRole.MEMBER,
    )

    assert settings.default_memory_scope is MemoryScope.PRIVATE


# --- REST capture resolution ------------------------------------------------


def _ctx(*, api_key_memory_scope_keys: set[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = "user-123"
    ctx.organization_id = "org-1"
    ctx.api_key_memory_scope_keys = api_key_memory_scope_keys
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_default", "writable", "keys", "expected_scope", "expected_reason"),
    [
        (
            MemoryScope.PROJECT,
            {"project_oncall"},
            None,
            MemoryScope.PROJECT,
            "default_scope_project",
        ),
        (
            MemoryScope.PROJECT,
            set(),
            None,
            MemoryScope.PRIVATE,
            "default_scope_requires_contributor",
        ),
        (
            MemoryScope.PRIVATE,
            {"project_oncall"},
            None,
            MemoryScope.PRIVATE,
            "default_scope_project_private",
        ),
        (
            MemoryScope.PRIVATE,
            {"project_oncall"},
            {api_key_memory_scope_key("project", "project_oncall")},
            MemoryScope.PROJECT,
            "default_scope_credential",
        ),
    ],
)
async def test_rest_capture_resolves_against_the_project_default(
    project_default, writable, keys, expected_scope, expected_reason
) -> None:
    with (
        patch(
            "sibyl.api.routes.memory_auth.get_project_default_memory_scope",
            AsyncMock(return_value=project_default),
        ),
        patch(
            "sibyl.api.routes.memory_auth.list_accessible_project_graph_ids",
            AsyncMock(return_value=writable),
        ) as list_projects,
    ):
        resolved = await memory_auth.resolve_capture_default_scope(
            ctx=_ctx(api_key_memory_scope_keys=keys), project_id="project_oncall"
        )

    assert resolved.memory_scope is expected_scope
    assert resolved.reason == expected_reason
    assert resolved.scope_key == (
        "project_oncall" if expected_scope is MemoryScope.PROJECT else None
    )
    if list_projects.await_count:
        assert list_projects.await_args.kwargs["required_role"] is ProjectRole.CONTRIBUTOR


@pytest.mark.asyncio
async def test_rest_capture_without_a_project_skips_the_lookup() -> None:
    with patch(
        "sibyl.api.routes.memory_auth.get_project_default_memory_scope", AsyncMock()
    ) as lookup:
        resolved = await memory_auth.resolve_capture_default_scope(ctx=_ctx(), project_id=None)

    lookup.assert_not_awaited()
    assert resolved.memory_scope is MemoryScope.PRIVATE
    assert resolved.reason == "default_scope_no_project"


def test_capture_project_hint_prefers_project_id_then_metadata() -> None:
    assert (
        memory_auth.capture_project_hint(
            RawMemoryRememberRequest(raw_content="x", project_id="a", metadata={"project_id": "b"})
        )
        == "a"
    )
    assert (
        memory_auth.capture_project_hint(
            RawMemoryRememberRequest(raw_content="x", metadata={"project_id": "b"})
        )
        == "b"
    )
    assert memory_auth.capture_project_hint(RawMemoryRememberRequest(raw_content="x")) is None


# --- MCP capture resolution -------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_capture_in_an_unshared_project_lands_private(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_context_module, "get_writable_projects", AsyncMock(return_value={"project_oncall"})
    )
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])
    with patch(
        "sibyl.mcp_tools.policy.get_project_default_memory_scope",
        AsyncMock(return_value=MemoryScope.PRIVATE),
    ):
        resolved = await mcp_policy.resolve_capture_default_scope(ctx, project="project_oncall")

    assert resolved.memory_scope is MemoryScope.PRIVATE
    assert resolved.scope_key is None


@pytest.mark.asyncio
async def test_mcp_capture_in_a_shared_project_lands_in_the_project(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_context_module, "get_writable_projects", AsyncMock(return_value={"project_oncall"})
    )
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])
    with patch(
        "sibyl.mcp_tools.policy.get_project_default_memory_scope",
        AsyncMock(return_value=MemoryScope.PROJECT),
    ):
        resolved = await mcp_policy.resolve_capture_default_scope(ctx, project="project_oncall")

    assert resolved.memory_scope is MemoryScope.PROJECT
    assert resolved.scope_key == "project_oncall"
    assert resolved.reason == "default_scope_project"


@pytest.mark.asyncio
async def test_mcp_key_narrowed_to_the_project_still_lands_there(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_context_module, "get_writable_projects", AsyncMock(return_value={"project_oncall"})
    )
    ctx = McpContext(
        org_id=str(uuid4()),
        user_id=str(uuid4()),
        scopes=["mcp"],
        api_key_memory_scope_keys=[api_key_memory_scope_key("project", "project_oncall")],
    )
    with patch(
        "sibyl.mcp_tools.policy.get_project_default_memory_scope",
        AsyncMock(return_value=MemoryScope.PRIVATE),
    ):
        resolved = await mcp_policy.resolve_capture_default_scope(ctx, project="project_oncall")

    assert resolved.memory_scope is MemoryScope.PROJECT
    assert resolved.reason == "default_scope_credential"
