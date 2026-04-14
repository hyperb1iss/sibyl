"""Tests for task CLI commands."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli import task


@patch("sibyl_cli.task.get_client")
def test_task_create_accepts_legacy_sync_flag(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.create_task = AsyncMock(
        return_value={"success": True, "task_id": "13364346-8475-4664-8b52-eb963af2fda7"}
    )
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(
        task.app,
        [
            "create",
            "--title",
            "Restore compatibility",
            "--project",
            "project_123456789abc",
            "--priority",
            "high",
            "--sync",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["id"] == "13364346-8475-4664-8b52-eb963af2fda7"
    assert payload["name"] == "Restore compatibility"
    assert payload["metadata"]["priority"] == "high"
    assert payload["metadata"]["project_id"] == "project_123456789abc"
    mock_client.create_task.assert_awaited_once_with(
        title="Restore compatibility",
        project_id="project_123456789abc",
        description=None,
        priority="high",
        complexity="medium",
        assignees=None,
        epic_id=None,
        feature=None,
        tags=None,
        technologies=None,
        depends_on=None,
    )


def test_validate_task_id_accepts_api_uuid() -> None:
    task_id = "13364346-8475-4664-8b52-eb963af2fda7"

    assert task._validate_task_id(task_id) == task_id
