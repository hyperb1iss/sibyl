from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.persistence.legacy.auth import UserNotFoundError
from sibyl.server import McpContext, _get_accessible_projects
from sibyl_core.auth import AuthContext, AuthOrganization, AuthUser, OrganizationRole


class _AsyncSessionContext:
    def __init__(self, session: object) -> None:
        self._session = session

    async def __aenter__(self) -> object:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb


@pytest.mark.asyncio
async def test_accessible_projects_intersects_with_api_key_scope() -> None:
    user = AuthUser(id=uuid4(), email="nova@example.com", name="Nova")
    organization = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
    auth_ctx = AuthContext(
        user=user,
        organization=organization,
        org_role=OrganizationRole.ADMIN,
        scopes=frozenset({"api:read"}),
    )
    ctx = McpContext(
        org_id=str(organization.id),
        user_id=str(user.id),
        scopes=["api:read"],
        api_key_project_ids=["project-a", "project-b"],
    )
    session = object()
    resolver = AsyncMock()
    resolver.resolve.return_value = auth_ctx

    with (
        patch("sibyl.db.connection.get_session", return_value=_AsyncSessionContext(session)),
        patch(
            "sibyl.persistence.legacy.auth.LegacyAuthContextResolver.from_session",
            return_value=resolver,
        ),
        patch(
            "sibyl.auth.authorization.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project-b", "project-c"}),
        ),
    ):
        result = await _get_accessible_projects(ctx)

    assert result == {"project-b"}
    resolver.resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_accessible_projects_returns_empty_when_user_disappears() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["api:read"])
    session = object()
    resolver = AsyncMock()
    resolver.resolve.side_effect = UserNotFoundError("User not found")

    with patch("sibyl.db.connection.get_session", return_value=_AsyncSessionContext(session)), patch(
        "sibyl.persistence.legacy.auth.LegacyAuthContextResolver.from_session",
        return_value=resolver,
    ):
        result = await _get_accessible_projects(ctx)

    assert result == set()
