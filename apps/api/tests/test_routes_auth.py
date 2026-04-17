from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes import auth as auth_routes
from sibyl_core.auth import AuthContext, AuthOrganization, AuthUser, OrganizationRole


def _ctx(*, include_org: bool = True) -> AuthContext:
    user = AuthUser(
        id=uuid4(),
        email="nova@example.com",
        name="Nova",
        github_id=42,
        is_admin=True,
        avatar_url="https://example.com/avatar.png",
    )
    organization = (
        AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
        if include_org
        else None
    )
    return AuthContext(
        user=user,
        organization=organization,
        org_role=OrganizationRole.ADMIN if include_org else None,
        scopes=frozenset({"api:write"}),
    )


@pytest.mark.asyncio
async def test_list_api_keys_uses_legacy_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    key = SimpleNamespace(
        id=uuid4(),
        name="CLI",
        key_prefix="sk_live_abcd",
        scopes=["mcp"],
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
        created_at=None,
    )
    list_keys = AsyncMock(return_value=[key])
    monkeypatch.setattr(auth_routes, "list_legacy_api_keys_for_user", list_keys)

    response = await auth_routes.list_api_keys(ctx=ctx)

    assert response["keys"][0]["name"] == "CLI"
    list_keys.assert_awaited_once_with(
        organization_id=ctx.organization.id,
        user_id=ctx.user.id,
    )


@pytest.mark.asyncio
async def test_create_api_key_uses_legacy_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    request = SimpleNamespace()
    record = SimpleNamespace(
        id=uuid4(),
        name="CLI",
        key_prefix="sk_live_abcd",
        scopes=["mcp"],
        expires_at=None,
    )
    create_key = AsyncMock(return_value=(record, "raw-secret"))
    monkeypatch.setattr(auth_routes, "create_legacy_api_key_for_user", create_key)

    response = await auth_routes.create_api_key(
        request=request,
        body=auth_routes.ApiKeyCreateRequest(name="CLI"),
        ctx=ctx,
        _admin=None,
    )

    assert response["api_key"] == "raw-secret"
    create_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_api_key_rejects_missing_org(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(include_org=False)

    with pytest.raises(HTTPException, match="No organization context") as exc_info:
        await auth_routes.revoke_api_key(
            request=SimpleNamespace(),
            api_key_id=uuid4(),
            ctx=ctx,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_me_uses_auth_context_payload() -> None:
    ctx = _ctx()

    response = await auth_routes.me(ctx=ctx)

    assert response["user"]["email"] == "nova@example.com"
    assert response["organization"]["slug"] == "sibyl"
    assert response["org_role"] == "admin"


@pytest.mark.asyncio
async def test_update_me_uses_legacy_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    request = SimpleNamespace()
    updated_user = SimpleNamespace(
        id=ctx.user.id,
        github_id=ctx.user.github_id,
        email="updated@example.com",
        name="Updated Nova",
        avatar_url="https://example.com/new.png",
    )
    update_user = AsyncMock(return_value=updated_user)
    monkeypatch.setattr(auth_routes, "update_legacy_auth_user", update_user)

    response = await auth_routes.update_me(
        request=request,
        body=auth_routes.MeUpdateRequest(name="Updated Nova"),
        ctx=ctx,
    )

    assert response["user"]["email"] == "updated@example.com"
    update_user.assert_awaited_once()
