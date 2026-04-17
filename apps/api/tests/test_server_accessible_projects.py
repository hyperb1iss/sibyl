from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.server import (
    McpContext,
    _get_accessible_projects,
    _get_mcp_context,
    _require_owner_mcp_context,
)
from sibyl_core.auth import AuthOrganization, AuthUser


@pytest.mark.asyncio
async def test_accessible_projects_intersects_with_api_key_scope() -> None:
    user = AuthUser(id=uuid4(), email="nova@example.com", name="Nova")
    organization = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
    ctx = McpContext(
        org_id=str(organization.id),
        user_id=str(user.id),
        scopes=["api:read"],
        api_key_project_ids=["project-a", "project-b"],
    )
    resolve_projects = AsyncMock(return_value={"project-b"})

    with patch("sibyl.server.resolve_legacy_accessible_project_graph_ids", resolve_projects):
        result = await _get_accessible_projects(ctx)

    assert result == {"project-b"}
    resolve_projects.assert_awaited_once_with(
        user_id=ctx.user_id,
        org_id=ctx.org_id,
        scopes=ctx.scopes,
        api_key_project_ids=ctx.api_key_project_ids,
    )


@pytest.mark.asyncio
async def test_accessible_projects_returns_empty_when_user_disappears() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["api:read"])
    with patch(
        "sibyl.server.resolve_legacy_accessible_project_graph_ids",
        AsyncMock(return_value=set()),
    ):
        result = await _get_accessible_projects(ctx)

    assert result == set()


@pytest.mark.asyncio
async def test_get_mcp_context_uses_legacy_api_key_auth() -> None:
    raw = "sk_live_test_token"
    auth = SimpleNamespace(
        organization_id=uuid4(),
        user_id=uuid4(),
        scopes=["mcp"],
        project_ids=[uuid4()],
    )

    with (
        patch("sibyl.server.get_access_token", return_value=SimpleNamespace(token=raw)),
        patch("sibyl.server.authenticate_legacy_api_key", AsyncMock(return_value=auth)) as authenticate,
    ):
        result = await _get_mcp_context()

    assert result == McpContext(
        org_id=str(auth.organization_id),
        user_id=str(auth.user_id),
        scopes=["mcp"],
        api_key_project_ids=[str(auth.project_ids[0])],
    )
    authenticate.assert_awaited_once_with(raw)


@pytest.mark.asyncio
async def test_require_owner_mcp_context_uses_legacy_owner_check() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()))

    with patch(
        "sibyl.server.has_legacy_owner_membership",
        AsyncMock(return_value=True),
    ) as has_owner:
        await _require_owner_mcp_context(ctx)

    has_owner.assert_awaited_once_with(org_id=ctx.org_id, user_id=ctx.user_id)


@pytest.mark.asyncio
async def test_require_owner_mcp_context_rejects_non_owner() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()))

    with (
        patch(
            "sibyl.server.has_legacy_owner_membership",
            AsyncMock(return_value=False),
        ),
        pytest.raises(ValueError, match="OWNER role required for log access"),
    ):
        await _require_owner_mcp_context(ctx)
