"""Tests for project-level authorization module."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.auth import authorization as authorization_module
from sibyl.auth.authorization import (
    PROJECT_ROLE_LEVELS,
    ProjectAuthorizationError,
    _max_role,
    list_accessible_project_graph_ids,
    require_project_admin,
    require_project_read,
    require_project_role,
    require_project_write,
)
from sibyl_core.auth import OrganizationRole, ProjectRole


class TestRoleHierarchy:
    """Tests for role hierarchy and level mappings."""

    def test_role_levels_order(self) -> None:
        """Verify role levels are correctly ordered."""
        assert (
            PROJECT_ROLE_LEVELS[ProjectRole.VIEWER] < PROJECT_ROLE_LEVELS[ProjectRole.CONTRIBUTOR]
        )
        assert (
            PROJECT_ROLE_LEVELS[ProjectRole.CONTRIBUTOR]
            < PROJECT_ROLE_LEVELS[ProjectRole.MAINTAINER]
        )
        assert PROJECT_ROLE_LEVELS[ProjectRole.MAINTAINER] < PROJECT_ROLE_LEVELS[ProjectRole.OWNER]

    def test_max_role_single(self) -> None:
        """_max_role returns the only role when given one."""
        assert _max_role(ProjectRole.VIEWER) == ProjectRole.VIEWER
        assert _max_role(ProjectRole.OWNER) == ProjectRole.OWNER

    def test_max_role_multiple(self) -> None:
        """_max_role returns the highest role."""
        assert _max_role(ProjectRole.VIEWER, ProjectRole.CONTRIBUTOR) == ProjectRole.CONTRIBUTOR
        assert (
            _max_role(ProjectRole.VIEWER, ProjectRole.MAINTAINER, ProjectRole.CONTRIBUTOR)
            == ProjectRole.MAINTAINER
        )
        assert (
            _max_role(ProjectRole.OWNER, ProjectRole.VIEWER, ProjectRole.CONTRIBUTOR)
            == ProjectRole.OWNER
        )

    def test_max_role_with_none(self) -> None:
        """_max_role ignores None values."""
        assert _max_role(None, ProjectRole.VIEWER) == ProjectRole.VIEWER
        assert (
            _max_role(ProjectRole.CONTRIBUTOR, None, ProjectRole.VIEWER) == ProjectRole.CONTRIBUTOR
        )
        assert _max_role(None, None, ProjectRole.OWNER) == ProjectRole.OWNER

    def test_max_role_all_none(self) -> None:
        """_max_role returns None when all inputs are None."""
        assert _max_role(None) is None
        assert _max_role(None, None) is None
        assert _max_role() is None


class TestListAccessibleProjectGraphIds:
    """Tests for list_accessible_project_graph_ids."""

    @pytest.mark.asyncio
    async def test_public_wrapper_uses_runtime_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Public authorization surface delegates project visibility to auth runtime."""
        ctx = MagicMock()
        expected = {"project_alpha"}
        helper = AsyncMock(return_value=expected)
        mock_session = AsyncMock()

        monkeypatch.setattr(
            authorization_module,
            "list_runtime_accessible_project_graph_ids",
            helper,
        )

        result = await list_accessible_project_graph_ids(mock_session, ctx)

        assert result == expected
        helper.assert_awaited_once_with(ctx)
        mock_session.execute.assert_not_called()


class TestProjectAuthorizationError:
    """Tests for ProjectAuthorizationError."""

    def test_structured_detail(self) -> None:
        """Error contains structured detail."""
        error = ProjectAuthorizationError(
            project_id="proj_123",
            required_role=ProjectRole.CONTRIBUTOR,
            actual_role=ProjectRole.VIEWER,
        )

        assert error.status_code == 403
        assert error.detail["error"] == "project_access_denied"
        # Fields are nested under "details"
        assert error.detail["details"]["project_id"] == "proj_123"
        assert error.detail["details"]["required_role"] == "project_contributor"
        assert error.detail["details"]["actual_role"] == "project_viewer"

    def test_no_access(self) -> None:
        """Error handles None actual_role."""
        error = ProjectAuthorizationError(
            project_id="proj_456",
            required_role=ProjectRole.VIEWER,
            actual_role=None,
        )

        # actual_role is set to None when no access
        assert error.detail["details"]["actual_role"] is None


class TestRequireProjectRole:
    """Tests for require_project_role dependency factory."""

    def test_creates_dependency(self) -> None:
        """Factory creates a callable dependency."""
        dep = require_project_role(ProjectRole.VIEWER)
        assert callable(dep)

    def test_require_project_read(self) -> None:
        """require_project_read creates correct dependency."""
        dep = require_project_read()
        assert callable(dep)

    def test_require_project_write(self) -> None:
        """require_project_write creates correct dependency."""
        dep = require_project_write()
        assert callable(dep)

    def test_require_project_admin(self) -> None:
        """require_project_admin creates correct dependency."""
        dep = require_project_admin()
        assert callable(dep)

    def test_custom_param_name(self) -> None:
        """Can customize project_id parameter name."""
        dep = require_project_role(ProjectRole.VIEWER, project_id_param="graph_id")
        assert callable(dep)

    def test_use_storage_uuid(self) -> None:
        """Can use the storage UUID instead of the graph ID."""
        dep = require_project_role(ProjectRole.VIEWER, use_graph_id=False)
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_dependency_uses_runtime_project_access(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dep = require_project_role(ProjectRole.CONTRIBUTOR)
        request = SimpleNamespace(path_params={"project_id": "project_abc123"}, query_params={})
        ctx = SimpleNamespace(
            organization=SimpleNamespace(id=uuid4()),
            user=SimpleNamespace(id=uuid4()),
            org_role=OrganizationRole.MEMBER,
        )
        project = SimpleNamespace(id=uuid4(), graph_project_id="project_abc123")
        resolve_project = AsyncMock(return_value=project)
        verify_access = AsyncMock(return_value=ProjectRole.CONTRIBUTOR)

        monkeypatch.setattr(
            authorization_module,
            "get_project_record_by_graph_id",
            resolve_project,
        )
        monkeypatch.setattr(
            authorization_module,
            "verify_runtime_entity_project_access",
            verify_access,
        )
        result = await dep(request=request, ctx=ctx)

        assert result is project
        resolve_project.assert_awaited_once_with(
            organization_id=ctx.organization.id,
            graph_project_id="project_abc123",
        )
        verify_access.assert_awaited_once_with(
            ctx=ctx,
            entity_project_id="project_abc123",
            required_role=ProjectRole.CONTRIBUTOR,
        )

    @pytest.mark.asyncio
    async def test_rejects_invalid_project_uuid(self) -> None:
        dep = require_project_role(ProjectRole.VIEWER, use_graph_id=False)
        request = SimpleNamespace(path_params={"project_id": "not-a-uuid"}, query_params={})
        ctx = SimpleNamespace(
            organization=SimpleNamespace(id=uuid4()),
            user=SimpleNamespace(id=uuid4()),
            org_role=OrganizationRole.MEMBER,
        )

        with pytest.raises(HTTPException) as exc_info:
            await dep(request=request, ctx=ctx)

        assert exc_info.value.status_code == 400
