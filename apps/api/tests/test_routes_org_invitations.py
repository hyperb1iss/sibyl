from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from starlette.requests import Request
from starlette.responses import Response

from sibyl.api.routes import org_invitations as invitation_routes
from sibyl.db.models import OrganizationRole
from sibyl.persistence.legacy.org_invitations import (
    LegacyInvitationAcceptance,
    LegacyInvitationRecord,
)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/orgs/electric-coven/invitations",
            "headers": [],
            "client": ("127.0.0.1", 3334),
        }
    )


@pytest.mark.asyncio
async def test_list_invitations_uses_legacy_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    list_invitations = AsyncMock(
        return_value=[
            LegacyInvitationRecord(
                id=UUID("00000000-0000-0000-0000-000000000222"),
                email="ember@example.com",
                role=OrganizationRole.ADMIN,
                created_at=datetime(2026, 4, 17, 12, 0, tzinfo=UTC),
                expires_at=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
            )
        ]
    )

    monkeypatch.setattr(invitation_routes, "list_legacy_org_invitations", list_invitations)

    payload = await invitation_routes.list_invitations(slug="electric-coven", user=user)

    list_invitations.assert_awaited_once_with(slug="electric-coven", actor_id=user.id)
    assert payload["invitations"][0]["role"] == "admin"


@pytest.mark.asyncio
async def test_create_invitation_uses_legacy_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    create_invitation = AsyncMock(
        return_value=LegacyInvitationRecord(
            id=UUID("00000000-0000-0000-0000-000000000222"),
            email="ember@example.com",
            role=OrganizationRole.ADMIN,
            expires_at=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
            accept_url="https://sibyl.test/api/invitations/token/accept",
        )
    )

    monkeypatch.setattr(invitation_routes, "create_legacy_org_invitation", create_invitation)

    payload = await invitation_routes.create_invitation(
        request=request,
        slug="electric-coven",
        body=invitation_routes.InvitationCreateRequest(
            email="ember@example.com",
            role=OrganizationRole.ADMIN,
            expires_days=7,
        ),
        user=user,
    )

    create_invitation.assert_awaited_once_with(
        slug="electric-coven",
        actor_id=user.id,
        email="ember@example.com",
        role=OrganizationRole.ADMIN,
        expires_days=7,
        request=request,
    )
    assert payload["invitation"]["accept_url"] == "https://sibyl.test/api/invitations/token/accept"


@pytest.mark.asyncio
async def test_delete_invitation_uses_legacy_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    invitation_id = UUID("00000000-0000-0000-0000-000000000222")
    delete_invitation = AsyncMock()

    monkeypatch.setattr(invitation_routes, "delete_legacy_org_invitation", delete_invitation)

    payload = await invitation_routes.delete_invitation(
        request=request,
        slug="electric-coven",
        invitation_id=invitation_id,
        user=user,
    )

    delete_invitation.assert_awaited_once_with(
        slug="electric-coven",
        actor_id=user.id,
        invitation_id=invitation_id,
        request=request,
    )
    assert payload == {"success": True}


@pytest.mark.asyncio
async def test_accept_invitation_sets_auth_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    response = Response()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    accept_invitation = AsyncMock(
        return_value=LegacyInvitationAcceptance(
            access_token="access-token",
            refresh_token="refresh-token",
            refresh_expires=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
            organization_id=UUID("00000000-0000-0000-0000-000000000333"),
            invitation_id=UUID("00000000-0000-0000-0000-000000000222"),
        )
    )
    set_auth_cookies = MagicMock()

    monkeypatch.setattr(invitation_routes, "accept_legacy_org_invitation", accept_invitation)
    monkeypatch.setattr(invitation_routes, "_set_auth_cookies", set_auth_cookies)

    payload = await invitation_routes.accept_invitation(
        request=request,
        token="invite-token",
        response=response,
        user=user,
    )

    accept_invitation.assert_awaited_once_with(token="invite-token", user=user, request=request)
    set_auth_cookies.assert_called_once_with(
        response,
        access_token="access-token",
        refresh_token="refresh-token",
        refresh_expires=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
    )
    assert payload == {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "token_type": "Bearer",
        "expires_in": invitation_routes.config_module.settings.access_token_expire_minutes * 60,
        "organization_id": "00000000-0000-0000-0000-000000000333",
    }
