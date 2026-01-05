"""Tests for project-level authorization module."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.auth.authorization import (
    PROJECT_ROLE_LEVELS,
    ProjectAuthorizationError,
    _max_role,
    get_effective_project_role,
    get_project_by_id,
    list_accessible_project_graph_ids,
    require_project_admin,
    require_project_read,
    require_project_role,
    require_project_write,
    resolve_project_by_graph_id,
    verify_entity_project_access,
)
from sibyl.db.models import (
    OrganizationRole,
    ProjectRole,
    ProjectVisibility,
)


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


class TestResolveProjectByGraphId:
    """Tests for resolve_project_by_graph_id."""

    @pytest.mark.asyncio
    async def test_found(self) -> None:
        """Returns project when found."""
        org_id = uuid4()
        project = MagicMock()
        project.id = uuid4()
        project.graph_project_id = "project_abc123"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = project

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        result = await resolve_project_by_graph_id(mock_session, org_id, "project_abc123")

        assert result == project
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        """Raises 404 when project not found."""
        org_id = uuid4()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await resolve_project_by_graph_id(mock_session, org_id, "nonexistent")

        assert exc_info.value.status_code == 404
        assert "nonexistent" in exc_info.value.detail


class TestGetProjectById:
    """Tests for get_project_by_id."""

    @pytest.mark.asyncio
    async def test_found(self) -> None:
        """Returns project when found."""
        org_id = uuid4()
        project_id = uuid4()
        project = MagicMock()
        project.id = project_id

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = project

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        result = await get_project_by_id(mock_session, org_id, project_id)

        assert result == project

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        """Raises 404 when project not found."""
        org_id = uuid4()
        project_id = uuid4()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await get_project_by_id(mock_session, org_id, project_id)

        assert exc_info.value.status_code == 404


class TestGetEffectiveProjectRole:
    """Tests for get_effective_project_role."""

    @pytest.fixture
    def mock_project(self) -> MagicMock:
        """Create a mock project."""
        project = MagicMock()
        project.id = uuid4()
        project.visibility = ProjectVisibility.PRIVATE
        project.default_role = ProjectRole.VIEWER
        return project

    @pytest.fixture
    def mock_user(self) -> MagicMock:
        """Create a mock user."""
        user = MagicMock()
        user.id = uuid4()
        return user

    @pytest.mark.asyncio
    async def test_org_owner_gets_project_owner(
        self, mock_project: MagicMock, mock_user: MagicMock
    ) -> None:
        """Org owner gets implicit project_owner."""
        ctx = MagicMock()
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.OWNER

        mock_session = AsyncMock()

        result = await get_effective_project_role(mock_session, ctx, mock_project)

        assert result == ProjectRole.OWNER
        # Should return early without querying DB
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_org_admin_gets_project_owner(
        self, mock_project: MagicMock, mock_user: MagicMock
    ) -> None:
        """Org admin gets implicit project_owner."""
        ctx = MagicMock()
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.ADMIN

        mock_session = AsyncMock()

        result = await get_effective_project_role(mock_session, ctx, mock_project)

        assert result == ProjectRole.OWNER
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_direct_membership(self, mock_project: MagicMock, mock_user: MagicMock) -> None:
        """Direct membership role is used."""
        ctx = MagicMock()
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.MEMBER

        # First query: direct membership
        direct_result = MagicMock()
        direct_result.scalar_one_or_none.return_value = ProjectRole.CONTRIBUTOR

        # Second query: team grants (empty)
        team_result = MagicMock()
        team_result.all.return_value = []

        mock_session = AsyncMock()
        mock_session.execute.side_effect = [direct_result, team_result]

        result = await get_effective_project_role(mock_session, ctx, mock_project)

        assert result == ProjectRole.CONTRIBUTOR

    @pytest.mark.asyncio
    async def test_team_grant_max(self, mock_project: MagicMock, mock_user: MagicMock) -> None:
        """Max team grant role is used."""
        ctx = MagicMock()
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.MEMBER

        # First query: no direct membership
        direct_result = MagicMock()
        direct_result.scalar_one_or_none.return_value = None

        # Second query: team grants - multiple roles
        team_result = MagicMock()
        team_result.all.return_value = [(ProjectRole.VIEWER,), (ProjectRole.MAINTAINER,)]

        mock_session = AsyncMock()
        mock_session.execute.side_effect = [direct_result, team_result]

        result = await get_effective_project_role(mock_session, ctx, mock_project)

        assert result == ProjectRole.MAINTAINER

    @pytest.mark.asyncio
    async def test_org_visibility_default(
        self, mock_project: MagicMock, mock_user: MagicMock
    ) -> None:
        """Org visibility grants default role."""
        mock_project.visibility = ProjectVisibility.ORG
        mock_project.default_role = ProjectRole.VIEWER

        ctx = MagicMock()
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.MEMBER

        # First query: no direct membership
        direct_result = MagicMock()
        direct_result.scalar_one_or_none.return_value = None

        # Second query: no team grants
        team_result = MagicMock()
        team_result.all.return_value = []

        mock_session = AsyncMock()
        mock_session.execute.side_effect = [direct_result, team_result]

        result = await get_effective_project_role(mock_session, ctx, mock_project)

        assert result == ProjectRole.VIEWER

    @pytest.mark.asyncio
    async def test_no_access(self, mock_project: MagicMock, mock_user: MagicMock) -> None:
        """Returns None when no access."""
        mock_project.visibility = ProjectVisibility.PRIVATE

        ctx = MagicMock()
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.MEMBER

        # No direct membership
        direct_result = MagicMock()
        direct_result.scalar_one_or_none.return_value = None

        # No team grants
        team_result = MagicMock()
        team_result.all.return_value = []

        mock_session = AsyncMock()
        mock_session.execute.side_effect = [direct_result, team_result]

        result = await get_effective_project_role(mock_session, ctx, mock_project)

        assert result is None


class TestListAccessibleProjectGraphIds:
    """Tests for list_accessible_project_graph_ids."""

    @pytest.mark.asyncio
    async def test_no_org_returns_empty(self) -> None:
        """Returns empty set when no org context."""
        ctx = MagicMock()
        ctx.organization = None

        mock_session = AsyncMock()

        result = await list_accessible_project_graph_ids(mock_session, ctx)

        assert result == set()

    @pytest.mark.asyncio
    async def test_org_admin_gets_all(self) -> None:
        """Org admin can access all projects."""
        ctx = MagicMock()
        ctx.organization = MagicMock()
        ctx.organization.id = uuid4()
        ctx.user = MagicMock()
        ctx.user.id = uuid4()
        ctx.org_role = OrganizationRole.OWNER

        # First query: migration check (returns a project to indicate not in migration mode)
        migration_result = MagicMock()
        migration_result.first.return_value = (uuid4(),)  # Project exists

        # Second query: get all projects
        projects_result = MagicMock()
        projects_result.all.return_value = [("proj_1",), ("proj_2",), ("proj_3",)]

        mock_session = AsyncMock()
        mock_session.execute.side_effect = [migration_result, projects_result]

        result = await list_accessible_project_graph_ids(mock_session, ctx)

        assert result == {"proj_1", "proj_2", "proj_3"}
        # Migration check + project list query
        assert mock_session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_member_gets_accessible(self) -> None:
        """Member gets combination of org-visible + direct + team."""
        ctx = MagicMock()
        ctx.organization = MagicMock()
        ctx.organization.id = uuid4()
        ctx.user = MagicMock()
        ctx.user.id = uuid4()
        ctx.org_role = OrganizationRole.MEMBER

        # First query: migration check (returns a project to indicate not in migration mode)
        migration_result = MagicMock()
        migration_result.first.return_value = (uuid4(),)  # Project exists

        # Org-visible projects
        org_result = MagicMock()
        org_result.all.return_value = [("proj_org1",), ("proj_org2",)]

        # Direct memberships
        direct_result = MagicMock()
        direct_result.all.return_value = [("proj_direct",)]

        # Team grants
        team_result = MagicMock()
        team_result.all.return_value = [("proj_team",), ("proj_org1",)]  # overlap with org

        mock_session = AsyncMock()
        mock_session.execute.side_effect = [
            migration_result,
            org_result,
            direct_result,
            team_result,
        ]

        result = await list_accessible_project_graph_ids(mock_session, ctx)

        assert result == {"proj_org1", "proj_org2", "proj_direct", "proj_team"}

    @pytest.mark.asyncio
    async def test_migration_mode_returns_none(self) -> None:
        """Returns None when no projects exist in Postgres (migration mode)."""
        ctx = MagicMock()
        ctx.organization = MagicMock()
        ctx.organization.id = uuid4()
        ctx.user = MagicMock()
        ctx.user.id = uuid4()
        ctx.org_role = OrganizationRole.MEMBER

        # Migration check returns None (no projects in Postgres)
        migration_result = MagicMock()
        migration_result.first.return_value = None

        mock_session = AsyncMock()
        mock_session.execute.return_value = migration_result

        result = await list_accessible_project_graph_ids(mock_session, ctx)

        # Should return None to indicate skip filtering
        assert result is None
        # Should only do the migration check query
        assert mock_session.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_migration_mode_no_org_role_returns_empty(self) -> None:
        """Returns empty set in migration mode when user has no org role."""
        ctx = MagicMock()
        ctx.organization = MagicMock()
        ctx.organization.id = uuid4()
        ctx.user = MagicMock()
        ctx.user.id = uuid4()
        ctx.org_role = None  # User has no org role

        # Migration check returns None (no projects in Postgres)
        migration_result = MagicMock()
        migration_result.first.return_value = None

        mock_session = AsyncMock()
        mock_session.execute.return_value = migration_result

        result = await list_accessible_project_graph_ids(mock_session, ctx)

        # Should return empty set (no access without org membership)
        assert result == set()


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

    def test_use_postgres_uuid(self) -> None:
        """Can use Postgres UUID instead of graph ID."""
        dep = require_project_role(ProjectRole.VIEWER, use_graph_id=False)
        assert callable(dep)


class TestVerifyEntityProjectAccess:
    """Tests for verify_entity_project_access write bypass fixes."""

    @pytest.fixture
    def mock_org(self) -> MagicMock:
        """Create a mock organization."""
        org = MagicMock()
        org.id = uuid4()
        return org

    @pytest.fixture
    def mock_user(self) -> MagicMock:
        """Create a mock user."""
        user = MagicMock()
        user.id = uuid4()
        return user

    @pytest.mark.asyncio
    async def test_unassigned_entity_viewer_allowed_for_member(
        self, mock_org: MagicMock, mock_user: MagicMock
    ) -> None:
        """Org member can VIEWER on unassigned entity."""
        ctx = MagicMock()
        ctx.organization = mock_org
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.MEMBER

        mock_session = AsyncMock()

        result = await verify_entity_project_access(
            mock_session, ctx, entity_project_id=None, required_role=ProjectRole.VIEWER
        )

        assert result == ProjectRole.VIEWER

    @pytest.mark.asyncio
    async def test_unassigned_entity_write_denied_for_member(
        self, mock_org: MagicMock, mock_user: MagicMock
    ) -> None:
        """Org member cannot CONTRIBUTOR on unassigned entity."""
        ctx = MagicMock()
        ctx.organization = mock_org
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.MEMBER

        mock_session = AsyncMock()

        with pytest.raises(ProjectAuthorizationError) as exc_info:
            await verify_entity_project_access(
                mock_session, ctx, entity_project_id=None, required_role=ProjectRole.CONTRIBUTOR
            )

        assert exc_info.value.detail["details"]["project_id"] == "unassigned"
        assert exc_info.value.detail["details"]["required_role"] == "project_contributor"

    @pytest.mark.asyncio
    async def test_unassigned_entity_write_allowed_for_admin(
        self, mock_org: MagicMock, mock_user: MagicMock
    ) -> None:
        """Org admin can do any operation on unassigned entity."""
        ctx = MagicMock()
        ctx.organization = mock_org
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.ADMIN

        mock_session = AsyncMock()

        result = await verify_entity_project_access(
            mock_session, ctx, entity_project_id=None, required_role=ProjectRole.MAINTAINER
        )

        assert result == ProjectRole.OWNER

    @pytest.mark.asyncio
    async def test_unsynced_project_viewer_allowed_for_member(
        self, mock_org: MagicMock, mock_user: MagicMock
    ) -> None:
        """Org member can VIEWER on unsynced project entity."""
        ctx = MagicMock()
        ctx.organization = mock_org
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.MEMBER

        mock_session = AsyncMock()
        # Make resolve_project_by_graph_id raise HTTPException (project not found)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await verify_entity_project_access(
            mock_session, ctx, entity_project_id="project_unsynced", required_role=ProjectRole.VIEWER
        )

        assert result == ProjectRole.VIEWER

    @pytest.mark.asyncio
    async def test_unsynced_project_write_denied_for_member(
        self, mock_org: MagicMock, mock_user: MagicMock
    ) -> None:
        """Org member cannot CONTRIBUTOR on unsynced project entity."""
        ctx = MagicMock()
        ctx.organization = mock_org
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.MEMBER

        mock_session = AsyncMock()
        # Make resolve_project_by_graph_id raise HTTPException (project not found)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(ProjectAuthorizationError) as exc_info:
            await verify_entity_project_access(
                mock_session,
                ctx,
                entity_project_id="project_unsynced",
                required_role=ProjectRole.CONTRIBUTOR,
            )

        assert exc_info.value.detail["details"]["project_id"] == "project_unsynced"
        assert exc_info.value.detail["details"]["required_role"] == "project_contributor"

    @pytest.mark.asyncio
    async def test_unsynced_project_write_allowed_for_admin(
        self, mock_org: MagicMock, mock_user: MagicMock
    ) -> None:
        """Org admin can do any operation on unsynced project entity."""
        ctx = MagicMock()
        ctx.organization = mock_org
        ctx.user = mock_user
        ctx.org_role = OrganizationRole.OWNER

        mock_session = AsyncMock()
        # Make resolve_project_by_graph_id raise HTTPException (project not found)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await verify_entity_project_access(
            mock_session,
            ctx,
            entity_project_id="project_unsynced",
            required_role=ProjectRole.MAINTAINER,
        )

        assert result == ProjectRole.OWNER
