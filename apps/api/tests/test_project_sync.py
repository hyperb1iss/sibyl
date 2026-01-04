"""Tests for project sync between graph and Postgres."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from sibyl.db.models import Project
from sibyl.db.project_sync import (
    _slugify,
    get_postgres_project_by_graph_id,
    sync_project_create,
    sync_project_delete,
    sync_project_update,
)


class TestSlugify:
    """Tests for slug generation."""

    def test_lowercase(self) -> None:
        """Converts to lowercase."""
        assert _slugify("My Project") == "my-project"

    def test_removes_special_chars(self) -> None:
        """Removes special characters and collapses resulting hyphens."""
        assert _slugify("Project @#$% Test!") == "project-test"

    def test_replaces_spaces_with_hyphens(self) -> None:
        """Replaces spaces with hyphens (underscores are stripped)."""
        assert _slugify("my project name") == "my-project-name"
        # Underscores become hyphens, then collapse
        assert _slugify("my_project") == "myproject"

    def test_collapses_multiple_hyphens(self) -> None:
        """Collapses multiple hyphens."""
        assert _slugify("my---project") == "my-project"

    def test_strips_leading_trailing_hyphens(self) -> None:
        """Strips leading and trailing hyphens."""
        assert _slugify("-project-") == "project"

    def test_truncates_to_64_chars(self) -> None:
        """Truncates to 64 characters."""
        long_name = "a" * 100
        assert len(_slugify(long_name)) == 64

    def test_empty_returns_project(self) -> None:
        """Empty string returns 'project'."""
        assert _slugify("") == "project"
        assert _slugify("@#$%") == "project"


class TestSyncProjectCreate:
    """Tests for project creation sync."""

    @pytest.mark.asyncio
    async def test_creates_postgres_row(self) -> None:
        """Creates Postgres project when graph project created."""
        session = AsyncMock()
        org_id = uuid4()
        user_id = uuid4()
        graph_id = "project_abc123"

        # Mock no existing project
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_result.first.return_value = None  # No slug collision
        session.execute.return_value = mock_result

        await sync_project_create(
            session,
            organization_id=org_id,
            owner_user_id=user_id,
            graph_project_id=graph_id,
            name="Test Project",
            description="A test project",
        )

        # Verify session.add was called with a Project
        session.add.assert_called_once()
        added_project = session.add.call_args[0][0]
        assert isinstance(added_project, Project)
        assert added_project.graph_project_id == graph_id
        assert added_project.name == "Test Project"
        assert added_project.organization_id == org_id
        assert added_project.owner_user_id == user_id
        session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_idempotent_if_exists(self) -> None:
        """Does not create duplicate if project already synced."""
        session = AsyncMock()
        org_id = uuid4()
        graph_id = "project_abc123"

        # Mock existing project
        existing = Project(
            id=uuid4(),
            organization_id=org_id,
            owner_user_id=uuid4(),
            name="Existing",
            slug="existing",
            graph_project_id=graph_id,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        session.execute.return_value = mock_result

        result = await sync_project_create(
            session,
            organization_id=org_id,
            owner_user_id=uuid4(),
            graph_project_id=graph_id,
            name="Test",
        )

        assert result == existing
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_truncates_description(self) -> None:
        """Truncates description to 2000 chars."""
        session = AsyncMock()
        org_id = uuid4()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_result.first.return_value = None
        session.execute.return_value = mock_result

        long_desc = "x" * 3000
        await sync_project_create(
            session,
            organization_id=org_id,
            owner_user_id=uuid4(),
            graph_project_id="project_test",
            name="Test",
            description=long_desc,
        )

        added_project = session.add.call_args[0][0]
        assert len(added_project.description) == 2000


class TestSyncProjectUpdate:
    """Tests for project update sync."""

    @pytest.mark.asyncio
    async def test_updates_name_and_slug(self) -> None:
        """Updates name and regenerates slug."""
        session = AsyncMock()
        org_id = uuid4()
        project_id = uuid4()
        graph_id = "project_abc123"

        existing = Project(
            id=project_id,
            organization_id=org_id,
            owner_user_id=uuid4(),
            name="Old Name",
            slug="old-name",
            graph_project_id=graph_id,
        )

        # First call returns existing project, second checks slug uniqueness
        call_count = [0]

        def mock_execute(query):
            result = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                result.scalar_one_or_none.return_value = existing
            else:
                result.first.return_value = None  # No slug collision
            return result

        session.execute = AsyncMock(side_effect=mock_execute)

        success = await sync_project_update(
            session,
            organization_id=org_id,
            graph_project_id=graph_id,
            name="New Name",
        )

        assert success is True
        # Verify update was called
        assert session.execute.call_count >= 2

    @pytest.mark.asyncio
    async def test_returns_false_if_not_found(self) -> None:
        """Returns False if project not in Postgres."""
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        success = await sync_project_update(
            session,
            organization_id=uuid4(),
            graph_project_id="project_missing",
            name="New Name",
        )

        assert success is False


class TestSyncProjectDelete:
    """Tests for project delete sync."""

    @pytest.mark.asyncio
    async def test_deletes_postgres_row(self) -> None:
        """Deletes Postgres project when graph project deleted."""
        session = AsyncMock()
        org_id = uuid4()
        graph_id = "project_abc123"

        mock_result = MagicMock()
        mock_result.rowcount = 1
        session.execute.return_value = mock_result

        success = await sync_project_delete(
            session,
            organization_id=org_id,
            graph_project_id=graph_id,
        )

        assert success is True
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_if_not_found(self) -> None:
        """Returns False if project not in Postgres."""
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.rowcount = 0
        session.execute.return_value = mock_result

        success = await sync_project_delete(
            session,
            organization_id=uuid4(),
            graph_project_id="project_missing",
        )

        assert success is False


class TestGetPostgresProjectByGraphId:
    """Tests for looking up Postgres project by graph ID."""

    @pytest.mark.asyncio
    async def test_returns_project_if_found(self) -> None:
        """Returns Project if found."""
        session = AsyncMock()
        org_id = uuid4()
        graph_id = "project_abc123"

        expected = Project(
            id=uuid4(),
            organization_id=org_id,
            owner_user_id=uuid4(),
            name="Test",
            slug="test",
            graph_project_id=graph_id,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expected
        session.execute.return_value = mock_result

        result = await get_postgres_project_by_graph_id(session, org_id, graph_id)

        assert result == expected

    @pytest.mark.asyncio
    async def test_returns_none_if_not_found(self) -> None:
        """Returns None if not found."""
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        result = await get_postgres_project_by_graph_id(
            session, uuid4(), "project_missing"
        )

        assert result is None
