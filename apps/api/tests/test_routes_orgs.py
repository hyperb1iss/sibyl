from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from starlette.requests import Request
from starlette.responses import Response

from sibyl.api.routes import orgs as org_routes
from sibyl.db.models import OrganizationRole
from sibyl.persistence.organization_common import OrgAuthResult, OrgRoleResult, OrgSummary


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/orgs",
            "headers": [],
            "client": ("127.0.0.1", 3334),
        }
    )


@pytest.mark.asyncio
async def test_list_orgs_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    list_orgs = AsyncMock(
        return_value=[
            OrgSummary(
                id=UUID("00000000-0000-0000-0000-000000000222"),
                slug="electric-coven",
                name="Electric Coven",
                is_personal=False,
                role=OrganizationRole.OWNER,
            )
        ]
    )

    monkeypatch.setattr(org_routes.organization_runtime, "list_orgs", list_orgs)

    payload = await org_routes.list_orgs(user=user)

    list_orgs.assert_awaited_once_with(user_id=user.id)
    assert payload["orgs"][0]["role"] == "owner"


@pytest.mark.asyncio
async def test_create_org_uses_runtime_helper_and_sets_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    response = Response()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    create_org = AsyncMock(
        return_value=OrgAuthResult(
            id=UUID("00000000-0000-0000-0000-000000000222"),
            slug="electric-coven",
            name="Electric Coven",
            access_token="access-token",
            refresh_token="refresh-token",
            refresh_expires=datetime.now(UTC) + timedelta(days=7),
        )
    )
    set_auth_cookies = MagicMock()

    monkeypatch.setattr(org_routes.organization_runtime, "create_org", create_org)
    monkeypatch.setattr(org_routes, "_set_auth_cookies", set_auth_cookies)

    payload = await org_routes.create_org(
        request=request,
        body=org_routes.OrganizationCreateRequest(name="Electric Coven"),
        response=response,
        user=user,
    )

    create_org.assert_awaited_once_with(
        request=request,
        user_id=user.id,
        name="Electric Coven",
        slug=None,
    )
    set_auth_cookies.assert_called_once()
    assert response.status_code == 201
    assert payload["organization"]["slug"] == "electric-coven"


@pytest.mark.asyncio
async def test_get_org_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = SimpleNamespace(user=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")))
    get_org = AsyncMock(
        return_value=OrgRoleResult(
            id=UUID("00000000-0000-0000-0000-000000000222"),
            slug="electric-coven",
            name="Electric Coven",
            role=OrganizationRole.ADMIN,
        )
    )

    monkeypatch.setattr(org_routes.organization_runtime, "get_org", get_org)

    payload = await org_routes.get_org(slug="electric-coven", ctx=ctx)

    get_org.assert_awaited_once_with(slug="electric-coven", user_id=ctx.user.id)
    assert payload["role"] == "admin"


@pytest.mark.asyncio
async def test_update_org_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    ctx = SimpleNamespace(user=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")))
    update_org = AsyncMock(
        return_value=OrgSummary(
            id=UUID("00000000-0000-0000-0000-000000000222"),
            slug="electric-coven-2",
            name="Electric Coven 2",
        )
    )

    monkeypatch.setattr(org_routes.organization_runtime, "update_org", update_org)

    payload = await org_routes.update_org(
        request=request,
        slug="electric-coven",
        body=org_routes.OrganizationUpdateRequest(name="Electric Coven 2", slug="electric-coven-2"),
        ctx=ctx,
    )

    update_org.assert_awaited_once_with(
        request=request,
        slug="electric-coven",
        user_id=ctx.user.id,
        name="Electric Coven 2",
        new_slug="electric-coven-2",
    )
    assert payload["organization"]["slug"] == "electric-coven-2"


@pytest.mark.asyncio
async def test_delete_org_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    ctx = SimpleNamespace(user=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")))
    delete_org = AsyncMock()

    monkeypatch.setattr(org_routes.organization_runtime, "delete_org", delete_org)

    response = await org_routes.delete_org(request=request, slug="electric-coven", ctx=ctx)

    delete_org.assert_awaited_once_with(
        request=request,
        slug="electric-coven",
        user_id=ctx.user.id,
    )
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_switch_org_uses_runtime_helper_and_sets_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    response = Response()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    switch_org = AsyncMock(
        return_value=OrgAuthResult(
            id=UUID("00000000-0000-0000-0000-000000000222"),
            slug="electric-coven",
            name="Electric Coven",
            access_token="access-token",
            refresh_token="refresh-token",
            refresh_expires=datetime.now(UTC) + timedelta(days=7),
        )
    )
    set_auth_cookies = MagicMock()

    monkeypatch.setattr(org_routes.organization_runtime, "switch_org", switch_org)
    monkeypatch.setattr(org_routes, "_set_auth_cookies", set_auth_cookies)

    payload = await org_routes.switch_org(
        request=request,
        slug="electric-coven",
        response=response,
        user=user,
    )

    switch_org.assert_awaited_once_with(
        request=request,
        slug="electric-coven",
        user_id=user.id,
    )
    set_auth_cookies.assert_called_once()
    assert payload["organization"]["name"] == "Electric Coven"
