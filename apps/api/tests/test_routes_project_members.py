"""Focused route tests for project member seams."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import BackgroundTasks

from sibyl.api.routes import project_members as project_member_routes
from sibyl.db.models import ProjectRole
from sibyl.persistence.organization_common import ProjectMemberChange, ProjectMembersResult


class TestCanManageMembers:
    def test_project_owner_can_manage(self) -> None:
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = user.id

        assert project_member_routes._can_manage_members(None, project, user) is True
        assert project_member_routes._can_manage_members(ProjectRole.VIEWER, project, user) is True

    def test_owner_role_can_manage(self) -> None:
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = uuid4()

        assert project_member_routes._can_manage_members(ProjectRole.OWNER, project, user) is True

    def test_maintainer_role_can_manage(self) -> None:
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = uuid4()

        assert (
            project_member_routes._can_manage_members(ProjectRole.MAINTAINER, project, user) is True
        )

    def test_contributor_cannot_manage(self) -> None:
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = uuid4()

        assert (
            project_member_routes._can_manage_members(ProjectRole.CONTRIBUTOR, project, user)
            is False
        )


@pytest.mark.asyncio
async def test_list_members_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    list_members = AsyncMock(
        return_value=ProjectMembersResult(
            members=[{"user": {"id": "u1"}, "role": "owner", "is_owner": True, "created_at": None}],
            can_manage=True,
        )
    )

    monkeypatch.setattr(
        project_member_routes.organization_runtime,
        "list_project_members",
        list_members,
    )

    payload = await project_member_routes.list_members(project_id="project_123", user=user, org=org)

    list_members.assert_awaited_once_with(project_id="project_123", actor=user, org_id=org.id)
    assert payload["can_manage"] is True


@pytest.mark.asyncio
async def test_add_member_uses_runtime_helper_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = MagicMock()
    background_tasks = BackgroundTasks()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    target_user_id = UUID("00000000-0000-0000-0000-000000000333")
    add_member = AsyncMock(
        return_value=ProjectMemberChange(
            org_id=org.id,
            project_db_id=UUID("00000000-0000-0000-0000-000000000444"),
            user_id=target_user_id,
            role=ProjectRole.MAINTAINER,
        )
    )
    broadcast = AsyncMock()

    monkeypatch.setattr(
        project_member_routes.organization_runtime,
        "add_project_member",
        add_member,
    )
    monkeypatch.setattr(project_member_routes, "broadcast_event", broadcast)

    payload = await project_member_routes.add_member(
        request=request,
        project_id="project_123",
        body=project_member_routes.MemberAddRequest(
            user_id=target_user_id,
            role=ProjectRole.MAINTAINER,
        ),
        background_tasks=background_tasks,
        user=user,
        org=org,
    )

    add_member.assert_awaited_once_with(
        request=request,
        project_id="project_123",
        actor=user,
        org_id=org.id,
        target_user_id=target_user_id,
        role=ProjectRole.MAINTAINER,
    )
    await background_tasks()
    broadcast.assert_awaited_once()
    assert payload == {
        "user_id": str(target_user_id),
        "role": ProjectRole.MAINTAINER.value,
    }


@pytest.mark.asyncio
async def test_update_member_role_uses_runtime_helper_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = MagicMock()
    background_tasks = BackgroundTasks()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    target_user_id = UUID("00000000-0000-0000-0000-000000000333")
    update_member = AsyncMock(
        return_value=ProjectMemberChange(
            org_id=org.id,
            project_db_id=UUID("00000000-0000-0000-0000-000000000444"),
            user_id=target_user_id,
            role=ProjectRole.VIEWER,
        )
    )
    broadcast = AsyncMock()

    monkeypatch.setattr(
        project_member_routes.organization_runtime,
        "update_project_member_role",
        update_member,
    )
    monkeypatch.setattr(project_member_routes, "broadcast_event", broadcast)

    payload = await project_member_routes.update_member_role(
        request=request,
        project_id="project_123",
        user_id=target_user_id,
        body=project_member_routes.MemberRoleUpdateRequest(role=ProjectRole.VIEWER),
        background_tasks=background_tasks,
        user=user,
        org=org,
    )

    update_member.assert_awaited_once_with(
        request=request,
        project_id="project_123",
        actor=user,
        org_id=org.id,
        target_user_id=target_user_id,
        role=ProjectRole.VIEWER,
    )
    await background_tasks()
    broadcast.assert_awaited_once()
    assert payload == {
        "user_id": str(target_user_id),
        "role": ProjectRole.VIEWER.value,
    }


@pytest.mark.asyncio
async def test_remove_member_uses_runtime_helper_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = MagicMock()
    background_tasks = BackgroundTasks()
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    target_user_id = UUID("00000000-0000-0000-0000-000000000333")
    remove_member = AsyncMock(
        return_value=ProjectMemberChange(
            org_id=org.id,
            project_db_id=UUID("00000000-0000-0000-0000-000000000444"),
            user_id=target_user_id,
        )
    )
    broadcast = AsyncMock()

    monkeypatch.setattr(
        project_member_routes.organization_runtime,
        "remove_project_member",
        remove_member,
    )
    monkeypatch.setattr(project_member_routes, "broadcast_event", broadcast)

    payload = await project_member_routes.remove_member(
        request=request,
        project_id="project_123",
        user_id=target_user_id,
        background_tasks=background_tasks,
        user=user,
        org=org,
    )

    remove_member.assert_awaited_once_with(
        request=request,
        project_id="project_123",
        actor=user,
        org_id=org.id,
        target_user_id=target_user_id,
    )
    await background_tasks()
    broadcast.assert_awaited_once()
    assert payload == {"success": True}
