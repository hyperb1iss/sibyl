"""Tests for project members endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.project_members import (
    MemberAddRequest,
    MemberRoleUpdateRequest,
    _can_manage_members,
    _get_project_and_user_role,
    add_member,
    list_members,
    remove_member,
    update_member_role,
)
from sibyl.db.models import ProjectRole


class TestCanManageMembers:
    """Tests for _can_manage_members helper."""

    def test_project_owner_can_manage(self) -> None:
        """Project owner can always manage."""
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = user.id

        assert _can_manage_members(None, project, user) is True
        assert _can_manage_members(ProjectRole.VIEWER, project, user) is True

    def test_owner_role_can_manage(self) -> None:
        """OWNER role can manage."""
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = uuid4()  # Different user

        assert _can_manage_members(ProjectRole.OWNER, project, user) is True

    def test_maintainer_role_can_manage(self) -> None:
        """MAINTAINER role can manage."""
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = uuid4()

        assert _can_manage_members(ProjectRole.MAINTAINER, project, user) is True

    def test_contributor_cannot_manage(self) -> None:
        """CONTRIBUTOR role cannot manage."""
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = uuid4()

        assert _can_manage_members(ProjectRole.CONTRIBUTOR, project, user) is False

    def test_viewer_cannot_manage(self) -> None:
        """VIEWER role cannot manage."""
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = uuid4()

        assert _can_manage_members(ProjectRole.VIEWER, project, user) is False

    def test_no_role_cannot_manage(self) -> None:
        """No role cannot manage (unless project owner)."""
        user = MagicMock()
        user.id = uuid4()
        project = MagicMock()
        project.owner_user_id = uuid4()

        assert _can_manage_members(None, project, user) is False


class TestGetProjectAndUserRole:
    """Tests for _get_project_and_user_role helper."""

    @pytest.mark.asyncio
    async def test_project_not_found(self) -> None:
        """Raises 404 when project not found."""
        user = MagicMock()
        org = MagicMock()
        org.id = uuid4()
        session = AsyncMock()
        session.get.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await _get_project_and_user_role(
                project_id=uuid4(), user=user, org=org, session=session
            )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_project_wrong_org(self) -> None:
        """Raises 404 when project belongs to different org."""
        user = MagicMock()
        org = MagicMock()
        org.id = uuid4()

        project = MagicMock()
        project.organization_id = uuid4()  # Different org

        session = AsyncMock()
        session.get.return_value = project

        with pytest.raises(HTTPException) as exc_info:
            await _get_project_and_user_role(
                project_id=uuid4(), user=user, org=org, session=session
            )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_project_owner_gets_owner_role(self) -> None:
        """Project owner gets OWNER role."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = user.id

        session = AsyncMock()
        session.get.return_value = project

        result_project, role = await _get_project_and_user_role(
            project_id=uuid4(), user=user, org=org, session=session
        )

        assert result_project == project
        assert role == ProjectRole.OWNER

    @pytest.mark.asyncio
    async def test_direct_member_gets_their_role(self) -> None:
        """Direct member gets their assigned role."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = uuid4()  # Different owner

        membership = MagicMock()
        membership.role = ProjectRole.CONTRIBUTOR

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = membership

        session = AsyncMock()
        session.get.return_value = project
        session.execute.return_value = mock_result

        result_project, role = await _get_project_and_user_role(
            project_id=uuid4(), user=user, org=org, session=session
        )

        assert result_project == project
        assert role == ProjectRole.CONTRIBUTOR

    @pytest.mark.asyncio
    async def test_non_member_gets_none(self) -> None:
        """Non-member gets None role."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = uuid4()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        session = AsyncMock()
        session.get.return_value = project
        session.execute.return_value = mock_result

        result_project, role = await _get_project_and_user_role(
            project_id=uuid4(), user=user, org=org, session=session
        )

        assert result_project == project
        assert role is None


class TestListMembers:
    """Tests for list_members endpoint."""

    @pytest.mark.asyncio
    async def test_returns_owner_first(self) -> None:
        """Owner is returned first in member list."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()

        owner = MagicMock()
        owner.id = uuid4()
        owner.email = "owner@example.com"
        owner.name = "Owner"
        owner.avatar_url = None

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = owner.id
        project.created_at = "2024-01-01T00:00:00Z"

        # Simulate member query returning empty (owner not in ProjectMember table)
        member_result = MagicMock()
        member_result.all.return_value = []

        # Simulate role check query returning user as MAINTAINER
        role_result = MagicMock()
        role_result.scalar_one_or_none.return_value = MagicMock(role=ProjectRole.MAINTAINER)

        session = AsyncMock()
        session.get.side_effect = lambda model, pk: project if model.__name__ == "Project" else owner
        session.execute.side_effect = [role_result, member_result]

        result = await list_members(
            project_id=project.id,
            user=user,
            org=org,
            session=session,
        )

        assert len(result["members"]) == 1
        assert result["members"][0]["is_owner"] is True
        assert result["members"][0]["role"] == ProjectRole.OWNER.value

    @pytest.mark.asyncio
    async def test_can_manage_flag(self) -> None:
        """can_manage flag reflects user's permissions."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = user.id  # User is owner
        project.created_at = "2024-01-01T00:00:00Z"

        member_result = MagicMock()
        member_result.all.return_value = []

        session = AsyncMock()
        session.get.side_effect = lambda model, pk: project if model.__name__ == "Project" else user
        session.execute.return_value = member_result

        result = await list_members(
            project_id=project.id,
            user=user,
            org=org,
            session=session,
        )

        assert result["can_manage"] is True


class TestAddMember:
    """Tests for add_member endpoint."""

    @pytest.mark.asyncio
    async def test_forbidden_without_manage_permission(self) -> None:
        """Returns 403 when user cannot manage members."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = uuid4()  # Different owner

        role_result = MagicMock()
        role_result.scalar_one_or_none.return_value = MagicMock(role=ProjectRole.VIEWER)

        session = AsyncMock()
        session.get.return_value = project
        session.execute.return_value = role_result

        request = MagicMock()
        body = MemberAddRequest(user_id=uuid4(), role=ProjectRole.CONTRIBUTOR)
        background = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await add_member(
                request=request,
                project_id=uuid4(),
                body=body,
                background_tasks=background,
                user=user,
                org=org,
                session=session,
            )

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_user_not_found(self) -> None:
        """Returns 404 when target user doesn't exist."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = user.id  # User is owner

        session = AsyncMock()
        # First get returns project, second get returns None (user not found)
        session.get.side_effect = [project, None]

        request = MagicMock()
        body = MemberAddRequest(user_id=uuid4(), role=ProjectRole.CONTRIBUTOR)
        background = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await add_member(
                request=request,
                project_id=uuid4(),
                body=body,
                background_tasks=background,
                user=user,
                org=org,
                session=session,
            )

        assert exc_info.value.status_code == 404
        assert "User not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_already_member_conflict(self) -> None:
        """Returns 409 when user is already a member."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()
        target_user_id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = user.id

        target_user = MagicMock()
        target_user.id = target_user_id

        existing_member = MagicMock()

        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = existing_member

        session = AsyncMock()
        session.get.side_effect = [project, target_user]
        session.execute.return_value = existing_result

        request = MagicMock()
        body = MemberAddRequest(user_id=target_user_id, role=ProjectRole.CONTRIBUTOR)
        background = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await add_member(
                request=request,
                project_id=uuid4(),
                body=body,
                background_tasks=background,
                user=user,
                org=org,
                session=session,
            )

        assert exc_info.value.status_code == 409
        assert "already a member" in exc_info.value.detail


class TestUpdateMemberRole:
    """Tests for update_member_role endpoint."""

    @pytest.mark.asyncio
    async def test_cannot_change_owner_role(self) -> None:
        """Returns 400 when trying to change project owner's role."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()
        owner_id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = owner_id

        # User is a maintainer (can manage, but can't change owner)
        role_result = MagicMock()
        role_result.scalar_one_or_none.return_value = MagicMock(role=ProjectRole.MAINTAINER)

        session = AsyncMock()
        session.get.return_value = project
        session.execute.return_value = role_result

        request = MagicMock()
        body = MemberRoleUpdateRequest(role=ProjectRole.CONTRIBUTOR)
        background = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await update_member_role(
                request=request,
                project_id=uuid4(),
                user_id=owner_id,  # Trying to change owner
                body=body,
                background_tasks=background,
                user=user,
                org=org,
                session=session,
            )

        assert exc_info.value.status_code == 400
        assert "owner" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_member_not_found(self) -> None:
        """Returns 404 when member doesn't exist."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()
        target_user_id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = user.id  # User is owner

        # Second query: member not found
        member_result = MagicMock()
        member_result.scalar_one_or_none.return_value = None

        session = AsyncMock()
        session.get.return_value = project
        session.execute.return_value = member_result

        request = MagicMock()
        body = MemberRoleUpdateRequest(role=ProjectRole.MAINTAINER)
        background = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await update_member_role(
                request=request,
                project_id=uuid4(),
                user_id=target_user_id,
                body=body,
                background_tasks=background,
                user=user,
                org=org,
                session=session,
            )

        assert exc_info.value.status_code == 404
        assert "Member not found" in exc_info.value.detail


class TestRemoveMember:
    """Tests for remove_member endpoint."""

    @pytest.mark.asyncio
    async def test_cannot_remove_owner(self) -> None:
        """Returns 400 when trying to remove project owner."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()
        owner_id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = owner_id

        role_result = MagicMock()
        role_result.scalar_one_or_none.return_value = MagicMock(role=ProjectRole.MAINTAINER)

        session = AsyncMock()
        session.get.return_value = project
        session.execute.return_value = role_result

        request = MagicMock()
        background = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await remove_member(
                request=request,
                project_id=uuid4(),
                user_id=owner_id,  # Trying to remove owner
                background_tasks=background,
                user=user,
                org=org,
                session=session,
            )

        assert exc_info.value.status_code == 400
        assert "owner" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_user_can_remove_self(self) -> None:
        """User can remove themselves even without manage permission."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = uuid4()  # Different owner

        membership = MagicMock()

        # First query: user's role (VIEWER - can't manage)
        role_result = MagicMock()
        role_result.scalar_one_or_none.return_value = MagicMock(role=ProjectRole.VIEWER)

        # Second query: find membership to delete
        member_result = MagicMock()
        member_result.scalar_one_or_none.return_value = membership

        session = AsyncMock()
        session.get.return_value = project
        session.execute.side_effect = [role_result, member_result]

        request = MagicMock()
        background = MagicMock()

        mock_audit_logger = MagicMock()
        mock_audit_logger.return_value.log = AsyncMock()

        with patch("sibyl.api.routes.project_members.AuditLogger", mock_audit_logger):
            result = await remove_member(
                request=request,
                project_id=uuid4(),
                user_id=user.id,  # Removing self
                background_tasks=background,
                user=user,
                org=org,
                session=session,
            )

        assert result["success"] is True
        session.delete.assert_called_once_with(membership)

    @pytest.mark.asyncio
    async def test_forbidden_removing_others_without_permission(self) -> None:
        """Returns 403 when removing others without manage permission."""
        user = MagicMock()
        user.id = uuid4()
        org = MagicMock()
        org.id = uuid4()
        other_user_id = uuid4()

        project = MagicMock()
        project.organization_id = org.id
        project.owner_user_id = uuid4()

        role_result = MagicMock()
        role_result.scalar_one_or_none.return_value = MagicMock(role=ProjectRole.VIEWER)

        session = AsyncMock()
        session.get.return_value = project
        session.execute.return_value = role_result

        request = MagicMock()
        background = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await remove_member(
                request=request,
                project_id=uuid4(),
                user_id=other_user_id,  # Removing someone else
                background_tasks=background,
                user=user,
                org=org,
                session=session,
            )

        assert exc_info.value.status_code == 403
