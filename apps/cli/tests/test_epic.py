"""Tests for CLI epic commands.

Tests that epic commands properly wrap status/priority/assignees in metadata
when calling the API, since EntityUpdate only supports top-level fields
(name, description, content, category, languages, tags, metadata).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl_cli import epic


class TestEpicUpdateMetadata:
    """Test that epic update commands wrap fields in metadata correctly."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create a mock SibylClient."""
        client = MagicMock()
        client.update_entity = AsyncMock(return_value={"id": "epic_123456789abc", "success": True})
        return client

    @patch("sibyl_cli.epic.get_client")
    def test_update_status_wrapped_in_metadata(self, mock_get_client: MagicMock) -> None:
        """Epic update --status should be wrapped in metadata."""
        mock_client = MagicMock()
        mock_client.update_entity = AsyncMock(return_value={"id": "epic_123456789abc"})
        mock_get_client.return_value = mock_client

        from typer.testing import CliRunner

        runner = CliRunner()
        runner.invoke(epic.app, ["update", "epic_123456789abc", "--status", "completed"])

        # Should have called update_entity with metadata containing status
        mock_client.update_entity.assert_called_once()
        call_kwargs = mock_client.update_entity.call_args
        assert "metadata" in call_kwargs.kwargs
        assert call_kwargs.kwargs["metadata"]["status"] == "completed"

    @patch("sibyl_cli.epic.get_client")
    def test_update_priority_wrapped_in_metadata(self, mock_get_client: MagicMock) -> None:
        """Epic update --priority should be wrapped in metadata."""
        mock_client = MagicMock()
        mock_client.update_entity = AsyncMock(return_value={"id": "epic_123456789abc"})
        mock_get_client.return_value = mock_client

        from typer.testing import CliRunner

        runner = CliRunner()
        runner.invoke(epic.app, ["update", "epic_123456789abc", "--priority", "high"])

        mock_client.update_entity.assert_called_once()
        call_kwargs = mock_client.update_entity.call_args
        assert call_kwargs.kwargs["metadata"]["priority"] == "high"

    @patch("sibyl_cli.epic.get_client")
    def test_update_title_is_top_level(self, mock_get_client: MagicMock) -> None:
        """Epic update --title should be a top-level 'name' field."""
        mock_client = MagicMock()
        mock_client.update_entity = AsyncMock(return_value={"id": "epic_123456789abc"})
        mock_get_client.return_value = mock_client

        from typer.testing import CliRunner

        runner = CliRunner()
        runner.invoke(epic.app, ["update", "epic_123456789abc", "--title", "New Title"])

        mock_client.update_entity.assert_called_once()
        call_kwargs = mock_client.update_entity.call_args
        assert call_kwargs.kwargs.get("name") == "New Title"

    @patch("sibyl_cli.epic.get_client")
    def test_start_wraps_status_in_metadata(self, mock_get_client: MagicMock) -> None:
        """Epic start should wrap status in metadata."""
        mock_client = MagicMock()
        mock_client.update_entity = AsyncMock(return_value={"id": "epic_123456789abc"})
        mock_get_client.return_value = mock_client

        from typer.testing import CliRunner

        runner = CliRunner()
        runner.invoke(epic.app, ["start", "epic_123456789abc"])

        mock_client.update_entity.assert_called_once()
        call_kwargs = mock_client.update_entity.call_args
        assert call_kwargs.kwargs["metadata"]["status"] == "in_progress"

    @patch("sibyl_cli.epic.get_client")
    def test_complete_wraps_status_in_metadata(self, mock_get_client: MagicMock) -> None:
        """Epic complete should wrap status and learnings in metadata."""
        mock_client = MagicMock()
        mock_client.update_entity = AsyncMock(return_value={"id": "epic_123456789abc"})
        mock_get_client.return_value = mock_client

        from typer.testing import CliRunner

        runner = CliRunner()
        runner.invoke(epic.app, ["complete", "epic_123456789abc", "--learnings", "Key insight"])

        mock_client.update_entity.assert_called_once()
        call_kwargs = mock_client.update_entity.call_args
        assert call_kwargs.kwargs["metadata"]["status"] == "completed"
        assert call_kwargs.kwargs["metadata"]["learnings"] == "Key insight"

    @patch("sibyl_cli.epic.get_client")
    def test_archive_wraps_status_in_metadata(self, mock_get_client: MagicMock) -> None:
        """Epic archive should wrap status and reason in metadata."""
        mock_client = MagicMock()
        mock_client.update_entity = AsyncMock(return_value={"id": "epic_123456789abc"})
        mock_get_client.return_value = mock_client

        from typer.testing import CliRunner

        runner = CliRunner()
        runner.invoke(epic.app, ["archive", "epic_123456789abc", "--reason", "Superseded"])

        mock_client.update_entity.assert_called_once()
        call_kwargs = mock_client.update_entity.call_args
        assert call_kwargs.kwargs["metadata"]["status"] == "archived"
        assert "Superseded" in call_kwargs.kwargs["metadata"]["learnings"]


class TestEpicTasksCommand:
    """Test epic tasks command uses proper API filtering."""

    @patch("sibyl_cli.epic.get_client")
    def test_tasks_uses_epic_filter(self, mock_get_client: MagicMock) -> None:
        """Epic tasks should use mode=list with epic= filter, not mode=related."""
        mock_client = MagicMock()
        mock_client.explore = AsyncMock(return_value={"entities": []})
        mock_get_client.return_value = mock_client

        from typer.testing import CliRunner

        runner = CliRunner()
        runner.invoke(epic.app, ["tasks", "epic_123456789abc"])

        mock_client.explore.assert_called_once()
        call_kwargs = mock_client.explore.call_args
        assert call_kwargs.kwargs["mode"] == "list"
        assert call_kwargs.kwargs["epic"] == "epic_123456789abc"
        assert call_kwargs.kwargs["types"] == ["task"]

    @patch("sibyl_cli.epic.get_client")
    def test_tasks_passes_status_filter(self, mock_get_client: MagicMock) -> None:
        """Epic tasks --status should pass status filter to API."""
        mock_client = MagicMock()
        mock_client.explore = AsyncMock(return_value={"entities": []})
        mock_get_client.return_value = mock_client

        from typer.testing import CliRunner

        runner = CliRunner()
        runner.invoke(epic.app, ["tasks", "epic_123456789abc", "--status", "todo"])

        call_kwargs = mock_client.explore.call_args
        assert call_kwargs.kwargs["status"] == "todo"


class TestEpicIdValidation:
    """Test epic ID validation."""

    def test_validate_epic_id_accepts_valid_id(self) -> None:
        """Valid epic IDs should pass validation."""
        result = epic._validate_epic_id("epic_123456789abc")
        assert result == "epic_123456789abc"

    def test_validate_epic_id_rejects_wrong_prefix(self) -> None:
        """IDs without epic_ prefix should be rejected."""
        from sibyl_cli.client import SibylClientError

        with pytest.raises(SibylClientError) as exc_info:
            epic._validate_epic_id("task_123456789abc")
        assert "Invalid epic ID format" in str(exc_info.value)

    def test_validate_epic_id_rejects_short_id(self) -> None:
        """Short epic IDs should be rejected."""
        from sibyl_cli.client import SibylClientError

        with pytest.raises(SibylClientError) as exc_info:
            epic._validate_epic_id("epic_123")
        assert "too short" in str(exc_info.value)
