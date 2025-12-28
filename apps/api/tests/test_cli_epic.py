"""Tests for Epic CLI commands."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl.cli.epic import _resolve_epic_id, app, format_epic_status

runner = CliRunner()


class TestFormatEpicStatus:
    """Tests for format_epic_status helper."""

    def test_planning_status(self) -> None:
        result = format_epic_status("planning")
        assert "planning" in result
        assert "#80ffea" in result  # NEON_CYAN

    def test_in_progress_status(self) -> None:
        result = format_epic_status("in_progress")
        assert "in_progress" in result
        assert "#e135ff" in result  # ELECTRIC_PURPLE

    def test_blocked_status(self) -> None:
        result = format_epic_status("blocked")
        assert "blocked" in result
        assert "#ff6363" in result  # Error red

    def test_completed_status(self) -> None:
        result = format_epic_status("completed")
        assert "completed" in result
        assert "#50fa7b" in result  # Success green

    def test_archived_status(self) -> None:
        result = format_epic_status("archived")
        assert "archived" in result
        assert "#888888" in result  # Gray

    def test_unknown_status(self) -> None:
        result = format_epic_status("unknown")
        assert "unknown" in result
        assert "#888888" in result  # Default gray


class TestResolveEpicId:
    """Tests for _resolve_epic_id helper."""

    @pytest.mark.asyncio
    async def test_full_id_returned_unchanged(self) -> None:
        """Full epic IDs (17+ chars) should be returned as-is."""
        mock_client = MagicMock()
        full_id = "epic_1234567890ab"  # 17 chars
        result = await _resolve_epic_id(mock_client, full_id)
        assert result == full_id
        # Should not call API
        mock_client.list_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_prefix_resolves_single_match(self) -> None:
        """Short prefix should resolve when single match found."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(
            return_value={
                "entities": [
                    {"id": "epic_abc123456789"},
                    {"id": "epic_xyz987654321"},
                ]
            }
        )

        result = await _resolve_epic_id(mock_client, "epic_abc")
        assert result == "epic_abc123456789"

    @pytest.mark.asyncio
    async def test_short_prefix_no_match_raises(self) -> None:
        """Short prefix with no matches should raise error."""
        from sibyl.cli.client import SibylClientError

        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(
            return_value={
                "entities": [
                    {"id": "epic_xyz987654321"},
                ]
            }
        )

        with pytest.raises(SibylClientError) as exc_info:
            await _resolve_epic_id(mock_client, "epic_abc")
        assert "No epic found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_short_prefix_multiple_matches_raises(self) -> None:
        """Short prefix with multiple matches should raise error."""
        from sibyl.cli.client import SibylClientError

        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(
            return_value={
                "entities": [
                    {"id": "epic_abc123456789"},
                    {"id": "epic_abc987654321"},
                ]
            }
        )

        with pytest.raises(SibylClientError) as exc_info:
            await _resolve_epic_id(mock_client, "epic_abc")
        assert "Multiple epics match" in str(exc_info.value)


class TestListEpicsCommand:
    """Tests for epic list command."""

    def test_list_epics_json_output(self) -> None:
        """List epics should output JSON by default."""
        mock_client = MagicMock()
        mock_client.explore = AsyncMock(
            return_value={
                "entities": [
                    {
                        "id": "epic_123",
                        "name": "Auth Epic",
                        "metadata": {"status": "in_progress", "priority": "high"},
                    }
                ]
            }
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["list", "--all"])

        assert result.exit_code == 0
        assert "epic_123" in result.stdout or "Auth Epic" in result.stdout

    def test_list_epics_with_status_filter(self) -> None:
        """List epics should filter by status."""
        mock_client = MagicMock()
        mock_client.explore = AsyncMock(
            return_value={
                "entities": [
                    {
                        "id": "epic_123",
                        "name": "In Progress Epic",
                        "metadata": {"status": "in_progress"},
                    },
                    {
                        "id": "epic_456",
                        "name": "Planning Epic",
                        "metadata": {"status": "planning"},
                    },
                ]
            }
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["list", "--status", "in_progress", "--all"])

        assert result.exit_code == 0
        # Should only show in_progress epic in output
        assert "epic_123" in result.stdout


class TestShowEpicCommand:
    """Tests for epic show command."""

    def test_show_epic_json_output(self) -> None:
        """Show epic should output JSON by default."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.get_entity = AsyncMock(
            return_value={
                "id": "epic_1234567890ab",
                "name": "Test Epic",
                "description": "Epic description",
                "metadata": {
                    "status": "in_progress",
                    "priority": "high",
                    "project_id": "proj_123",
                },
            }
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["show", "epic_1234567890ab"])

        assert result.exit_code == 0
        assert "Test Epic" in result.stdout


class TestCreateEpicCommand:
    """Tests for epic create command."""

    def test_create_epic_json_output(self) -> None:
        """Create epic should output JSON by default."""
        mock_client = MagicMock()
        mock_client.create_entity = AsyncMock(
            return_value={"id": "epic_new123456789", "name": "New Epic"}
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["create", "--title", "New Epic", "--project", "proj_123"])

        assert result.exit_code == 0
        assert "epic_new" in result.stdout

    def test_create_epic_with_options(self) -> None:
        """Create epic with all options."""
        mock_client = MagicMock()
        mock_client.create_entity = AsyncMock(return_value={"id": "epic_new123456789"})

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "create",
                    "--title",
                    "Full Epic",
                    "--project",
                    "proj_123",
                    "--priority",
                    "high",
                    "--assignee",
                    "alice",
                    "--tags",
                    "security,auth",
                ],
            )

        assert result.exit_code == 0
        # Verify create_entity was called with correct metadata
        call_kwargs = mock_client.create_entity.call_args[1]
        assert call_kwargs["metadata"]["priority"] == "high"
        assert call_kwargs["metadata"]["assignees"] == ["alice"]
        assert call_kwargs["metadata"]["tags"] == ["security", "auth"]


class TestStartEpicCommand:
    """Tests for epic start command."""

    def test_start_epic_json_output(self) -> None:
        """Start epic should output JSON by default."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.update_entity = AsyncMock(
            return_value={"id": "epic_1234567890ab", "success": True}
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["start", "epic_1234567890ab"])

        assert result.exit_code == 0
        # Verify update was called with in_progress status
        call_args = mock_client.update_entity.call_args[0]
        assert call_args[1]["status"] == "in_progress"


class TestCompleteEpicCommand:
    """Tests for epic complete command."""

    def test_complete_epic_json_output(self) -> None:
        """Complete epic should output JSON by default."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.update_entity = AsyncMock(
            return_value={"id": "epic_1234567890ab", "success": True}
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["complete", "epic_1234567890ab"])

        assert result.exit_code == 0
        call_args = mock_client.update_entity.call_args[0]
        assert call_args[1]["status"] == "completed"

    def test_complete_epic_with_learnings(self) -> None:
        """Complete epic should capture learnings."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.update_entity = AsyncMock(
            return_value={"id": "epic_1234567890ab", "success": True}
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(
                app,
                ["complete", "epic_1234567890ab", "--learnings", "OAuth needs exact redirect URIs"],
            )

        assert result.exit_code == 0
        call_args = mock_client.update_entity.call_args[0]
        assert call_args[1]["learnings"] == "OAuth needs exact redirect URIs"


class TestUpdateEpicCommand:
    """Tests for epic update command."""

    def test_update_epic_no_fields_errors(self) -> None:
        """Update epic without fields should error."""
        mock_client = MagicMock()

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["update", "epic_1234567890ab"])

        # Should show error about no fields
        assert "No fields to update" in result.stdout

    def test_update_epic_with_status(self) -> None:
        """Update epic status."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.update_entity = AsyncMock(
            return_value={"id": "epic_1234567890ab", "success": True}
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["update", "epic_1234567890ab", "--status", "blocked"])

        assert result.exit_code == 0
        call_args = mock_client.update_entity.call_args[0]
        assert call_args[1]["status"] == "blocked"

    def test_update_epic_multiple_fields(self) -> None:
        """Update epic with multiple fields."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.update_entity = AsyncMock(
            return_value={"id": "epic_1234567890ab", "success": True}
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "update",
                    "epic_1234567890ab",
                    "--priority",
                    "critical",
                    "--assignee",
                    "bob",
                ],
            )

        assert result.exit_code == 0
        call_args = mock_client.update_entity.call_args[0]
        assert call_args[1]["priority"] == "critical"
        assert call_args[1]["assignees"] == ["bob"]


class TestArchiveEpicCommand:
    """Tests for epic archive command."""

    def test_archive_epic_with_confirm(self) -> None:
        """Archive epic with --yes flag."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.update_entity = AsyncMock(
            return_value={"id": "epic_1234567890ab", "success": True}
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["archive", "epic_1234567890ab", "--yes"])

        assert result.exit_code == 0
        call_args = mock_client.update_entity.call_args[0]
        assert call_args[1]["status"] == "archived"

    def test_archive_epic_with_reason(self) -> None:
        """Archive epic with reason."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.update_entity = AsyncMock(
            return_value={"id": "epic_1234567890ab", "success": True}
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(
                app,
                ["archive", "epic_1234567890ab", "--yes", "--reason", "Superseded"],
            )

        assert result.exit_code == 0
        call_args = mock_client.update_entity.call_args[0]
        assert "Superseded" in call_args[1]["learnings"]


class TestListEpicTasksCommand:
    """Tests for epic tasks command."""

    def test_list_epic_tasks_json_output(self) -> None:
        """List epic tasks should output JSON by default."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.explore = AsyncMock(
            return_value={
                "entities": [
                    {
                        "id": "task_123",
                        "name": "Implement OAuth",
                        "metadata": {
                            "epic_id": "epic_1234567890ab",
                            "status": "doing",
                        },
                    },
                    {
                        "id": "task_456",
                        "name": "Other Task",
                        "metadata": {
                            "epic_id": "epic_other",
                            "status": "todo",
                        },
                    },
                ]
            }
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["tasks", "epic_1234567890ab"])

        assert result.exit_code == 0
        # Should only show task belonging to this epic
        assert "task_123" in result.stdout
        assert "task_456" not in result.stdout

    def test_list_epic_tasks_with_status_filter(self) -> None:
        """List epic tasks filtered by status."""
        mock_client = MagicMock()
        mock_client.list_entities = AsyncMock(return_value={"entities": []})
        mock_client.explore = AsyncMock(
            return_value={
                "entities": [
                    {
                        "id": "task_123",
                        "name": "Doing Task",
                        "metadata": {
                            "epic_id": "epic_1234567890ab",
                            "status": "doing",
                        },
                    },
                    {
                        "id": "task_789",
                        "name": "Todo Task",
                        "metadata": {
                            "epic_id": "epic_1234567890ab",
                            "status": "todo",
                        },
                    },
                ]
            }
        )

        with patch("sibyl.cli.epic.get_client", return_value=mock_client):
            result = runner.invoke(app, ["tasks", "epic_1234567890ab", "--status", "doing"])

        assert result.exit_code == 0
        assert "task_123" in result.stdout
        assert "task_789" not in result.stdout
