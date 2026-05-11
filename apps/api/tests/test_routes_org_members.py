from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import BackgroundTasks
from starlette.requests import Request

from sibyl.api.routes import org_members as org_member_routes
from sibyl.db.models import OrganizationRole
from sibyl.persistence.organization_common import OrgMemberChange


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/orgs/electric-coven/members",
            "headers": [],
            "client": ("127.0.0.1", 3334),
        }
    )


@pytest.mark.asyncio
async def test_list_members_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    list_members = AsyncMock(
        return_value=[
            {
                "user": {
                    "id": "00000000-0000-0000-0000-000000000222",
                    "github_id": 123,
                    "email": "ember@example.com",
                    "name": "Ember",
                    "avatar_url": None,
                },
                "role": "member",
                "created_at": None,
            }
        ]
    )

    monkeypatch.setattr(org_member_routes.organization_runtime, "list_org_members", list_members)

    payload = await org_member_routes.list_members(slug="electric-coven", user=user)

    list_members.assert_awaited_once_with(slug="electric-coven", actor_id=user.id)
    assert payload["members"][0]["user"]["id"] == "00000000-0000-0000-0000-000000000222"


@pytest.mark.asyncio
async def test_add_member_uses_runtime_helper_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    background_tasks = BackgroundTasks()
    add_member = AsyncMock(
        return_value=OrgMemberChange(
            org_id=UUID("00000000-0000-0000-0000-000000000333"),
            user_id=UUID("00000000-0000-0000-0000-000000000222"),
            role=OrganizationRole.ADMIN,
        )
    )
    broadcast = AsyncMock()

    monkeypatch.setattr(org_member_routes.organization_runtime, "add_org_member", add_member)
    monkeypatch.setattr(org_member_routes, "broadcast_event", broadcast)

    payload = await org_member_routes.add_member(
        request=request,
        slug="electric-coven",
        body=org_member_routes.MemberAddRequest(
            user_id=UUID("00000000-0000-0000-0000-000000000222"),
            role=OrganizationRole.ADMIN,
        ),
        background_tasks=background_tasks,
        user=user,
    )

    add_member.assert_awaited_once_with(
        slug="electric-coven",
        actor_id=user.id,
        target_user_id=UUID("00000000-0000-0000-0000-000000000222"),
        role=OrganizationRole.ADMIN,
        request=request,
    )
    await background_tasks()
    broadcast.assert_awaited_once_with(
        "permission_changed",
        {
            "user_id": "00000000-0000-0000-0000-000000000222",
            "change_type": "org_member_added",
            "org_role": "admin",
        },
        org_id="00000000-0000-0000-0000-000000000333",
    )
    assert payload == {
        "user_id": "00000000-0000-0000-0000-000000000222",
        "role": "admin",
    }


@pytest.mark.asyncio
async def test_update_member_role_uses_runtime_helper_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    target_user_id = UUID("00000000-0000-0000-0000-000000000222")
    background_tasks = BackgroundTasks()
    update_member = AsyncMock(
        return_value=OrgMemberChange(
            org_id=UUID("00000000-0000-0000-0000-000000000333"),
            user_id=target_user_id,
            role=OrganizationRole.VIEWER,
        )
    )
    broadcast = AsyncMock()

    monkeypatch.setattr(
        org_member_routes.organization_runtime,
        "update_org_member_role",
        update_member,
    )
    monkeypatch.setattr(org_member_routes, "broadcast_event", broadcast)

    payload = await org_member_routes.update_member_role(
        request=request,
        slug="electric-coven",
        user_id=target_user_id,
        body=org_member_routes.MemberRoleUpdateRequest(role=OrganizationRole.VIEWER),
        background_tasks=background_tasks,
        user=user,
    )

    update_member.assert_awaited_once_with(
        slug="electric-coven",
        actor_id=user.id,
        target_user_id=target_user_id,
        role=OrganizationRole.VIEWER,
        request=request,
    )
    await background_tasks()
    broadcast.assert_awaited_once_with(
        "permission_changed",
        {
            "user_id": str(target_user_id),
            "change_type": "org_role_changed",
            "org_role": "viewer",
        },
        org_id="00000000-0000-0000-0000-000000000333",
    )
    assert payload == {"user_id": str(target_user_id), "role": "viewer"}


@pytest.mark.asyncio
async def test_remove_member_uses_runtime_helper_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    target_user_id = UUID("00000000-0000-0000-0000-000000000222")
    background_tasks = BackgroundTasks()
    remove_member = AsyncMock(
        return_value=OrgMemberChange(
            org_id=UUID("00000000-0000-0000-0000-000000000333"),
            user_id=target_user_id,
        )
    )
    broadcast = AsyncMock()

    monkeypatch.setattr(
        org_member_routes.organization_runtime,
        "remove_org_member",
        remove_member,
    )
    monkeypatch.setattr(org_member_routes, "broadcast_event", broadcast)

    payload = await org_member_routes.remove_member(
        request=request,
        slug="electric-coven",
        user_id=target_user_id,
        background_tasks=background_tasks,
        user=user,
    )

    remove_member.assert_awaited_once_with(
        slug="electric-coven",
        actor_id=user.id,
        target_user_id=target_user_id,
        request=request,
    )
    await background_tasks()
    broadcast.assert_awaited_once_with(
        "permission_changed",
        {
            "user_id": str(target_user_id),
            "change_type": "org_member_removed",
        },
        org_id="00000000-0000-0000-0000-000000000333",
    )
    assert payload == {"success": True}
