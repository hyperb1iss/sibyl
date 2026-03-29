"""Tests for CLI task note authoring flags."""

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli import task


class TestTaskNoteAuthorFlags:
    """Task note command should prefer assistant wording but keep compatibility."""

    @patch("sibyl_cli.task.get_client")
    def test_note_uses_agent_wire_value_for_assistant_flag(self, mock_get_client: MagicMock) -> None:
        """The preferred --assistant flag should still send the legacy wire value."""
        mock_client = MagicMock()
        mock_client.create_note = AsyncMock(return_value={"id": "note_123"})
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(
            task.app,
            [
                "note",
                "task_123456789abc",
                "Implemented fix",
                "--assistant",
                "--author",
                "claude",
            ],
        )

        assert result.exit_code == 0
        mock_client.create_note.assert_called_once_with(
            "task_123456789abc",
            "Implemented fix",
            "agent",
            "claude",
        )

    @patch("sibyl_cli.task.get_client")
    def test_note_keeps_agent_flag_as_backward_compatible_alias(
        self, mock_get_client: MagicMock
    ) -> None:
        """Older scripts using --agent should continue to work."""
        mock_client = MagicMock()
        mock_client.create_note = AsyncMock(return_value={"id": "note_123"})
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(
            task.app,
            [
                "note",
                "task_123456789abc",
                "Implemented fix",
                "--agent",
            ],
        )

        assert result.exit_code == 0
        mock_client.create_note.assert_called_once_with(
            "task_123456789abc",
            "Implemented fix",
            "agent",
            "",
        )
