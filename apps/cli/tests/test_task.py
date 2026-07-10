"""Tests for task CLI commands."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli import task
from sibyl_cli.client import SibylClientError
from sibyl_cli.main import app as main_app


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


@patch("sibyl_cli.task.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.task.get_client")
def test_top_level_tasks_alias_lists_tasks(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.explore = AsyncMock(return_value={"entities": [], "total": 0})
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(main_app, ["tasks", "--status", "doing"])

    assert result.exit_code == 0
    mock_client.explore.assert_awaited_once()
    assert mock_client.explore.await_args.kwargs["status"] == "doing"
    assert mock_client.explore.await_args.kwargs["project"] == "project_123"
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.task.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.task.get_client")
def test_task_list_accepts_wide_table_mode(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    title = "Audit render pipeline end-to-end without title fragmentation"
    mock_client = MagicMock()
    mock_client.explore = AsyncMock(
        return_value={
            "entities": [
                {
                    "id": "task_123456789abc",
                    "name": title,
                    "metadata": {
                        "status": "todo",
                        "priority": "high",
                        "assignees": [],
                        "project_id": "project_123",
                    },
                }
            ],
            "total": 1,
        }
    )
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(task.app, ["list", "--wide", "--status", "todo"])

    assert result.exit_code == 0
    assert title in result.stdout
    mock_client.explore.assert_awaited_once()
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.task.get_client")
def test_task_get_alias_resolves_to_show(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(
        return_value={
            "id": "task_123456789abc",
            "name": "Alias target",
            "description": "Body",
            "metadata": {"status": "todo", "priority": "medium"},
        }
    )
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(task.app, ["get", "task_123456789abc", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["name"] == "Alias target"
    mock_client.get_entity.assert_awaited_once_with("task_123456789abc")


@patch("sibyl_cli.task.get_client")
def test_task_complete_with_learnings_reports_queued_capture(
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.complete_task = AsyncMock(
        return_value={
            "success": True,
            "message": "Task completed with learnings captured",
            "data": {"status": "done", "learnings": "Reusable policy lesson"},
        }
    )
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(
        task.app,
        [
            "complete",
            "task_123456789abc",
            "--hours",
            "1.5",
            "--learnings",
            "Reusable policy lesson",
        ],
    )

    assert result.exit_code == 0
    assert "Task completed: task_123456789abc" in result.stdout
    assert "Task learning capture queued" in result.stdout
    mock_client.complete_task.assert_awaited_once_with(
        "task_123456789abc",
        1.5,
        "Reusable policy lesson",
    )


@patch("sibyl_cli.task.get_client")
def test_task_complete_with_cited_ids_reports_usage(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.complete_task = AsyncMock(
        return_value={
            "success": True,
            "message": "Task completed",
            "data": {
                "citation_usage": {
                    "cited_count": 2,
                    "stamped_count": 2,
                },
                "status": "done",
            },
        }
    )
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(
        task.app,
        [
            "complete",
            "task_123456789abc",
            "--cited",
            "decision-1,raw_memory:raw-1",
        ],
    )

    assert result.exit_code == 0
    assert "Citations recorded: 2/2" in result.stdout
    mock_client.complete_task.assert_awaited_once_with(
        "task_123456789abc",
        None,
        None,
        cited_ids=["decision-1", "raw_memory:raw-1"],
    )


@patch("sibyl_cli.task.get_client")
def test_task_complete_accepts_note_alias(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.complete_task = AsyncMock(return_value={"success": True})
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(task.app, ["complete", "task_123456789abc", "--note", "Alias lesson"])

    assert result.exit_code == 0
    mock_client.complete_task.assert_awaited_once_with(
        "task_123456789abc",
        None,
        "Alias lesson",
    )


@patch("sibyl_cli.task.get_client")
def test_task_complete_reads_learnings_file(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    learnings_file = tmp_path / "learnings.md"
    learnings_file.write_text("File-based lesson\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.complete_task = AsyncMock(return_value={"success": True})
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(
        task.app,
        [
            "complete",
            "task_123456789abc",
            "--learnings-file",
            str(learnings_file),
        ],
    )

    assert result.exit_code == 0
    assert "Task learning capture queued" in result.stdout
    mock_client.complete_task.assert_awaited_once_with(
        "task_123456789abc",
        None,
        "File-based lesson",
    )


@patch("sibyl_cli.task.get_client")
def test_task_note_reads_content_file(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    content_file = tmp_path / "diag.md"
    content_file.write_text("Root cause breadcrumb\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.create_note = AsyncMock(
        return_value={
            "success": True,
            "id": "note-1",
            "mutation_receipt": {
                "operation_id": "note-op-1",
                "applied": True,
                "revision": 1,
            },
        }
    )
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(
        task.app,
        [
            "note",
            "task_123456789abc",
            "--content-file",
            str(content_file),
            "--assistant",
            "--author",
            "Nova",
        ],
    )

    assert result.exit_code == 0
    mock_client.create_note.assert_awaited_once_with(
        "task_123456789abc",
        "Root cause breadcrumb",
        "agent",
        "Nova",
    )
    assert "Revision: 1" in result.stdout
    assert "Operation: note-op-1 (applied)" in result.stdout


@patch("sibyl_cli.task.get_client")
def test_task_note_reads_content_from_stdin(
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.create_note = AsyncMock(return_value={"success": True})
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(
        task.app,
        ["note", "task_123456789abc", "-"],
        input="Piped breadcrumb\n",
    )

    assert result.exit_code == 0
    mock_client.create_note.assert_awaited_once_with(
        "task_123456789abc",
        "Piped breadcrumb",
        "user",
        "",
    )


@patch("sibyl_cli.task.get_client")
def test_task_start_accepts_short_prefix(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.resolve_id_prefix = AsyncMock(
        return_value={"matches": [{"id": "task_123456789abc"}]}
    )
    mock_client.start_task = AsyncMock(return_value={"success": True})
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(task.app, ["start", "123456", "--json"])

    assert result.exit_code == 0
    mock_client.resolve_id_prefix.assert_awaited_once_with("123456", entity_type="task")
    mock_client.start_task.assert_awaited_once_with("task_123456789abc", None)


@patch("sibyl_cli.task.get_client")
def test_task_start_forwards_expected_revision_and_prints_receipt(
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.start_task = AsyncMock(
        return_value={
            "success": True,
            "mutation_receipt": {
                "operation_id": "start-1",
                "applied": True,
                "revision": 8,
            },
        }
    )
    mock_get_client.return_value = mock_client
    mock_client.resolve_id_prefix = AsyncMock(
        return_value={"matches": [{"id": "task_123456789abc"}]}
    )
    mock_client.get_entity = AsyncMock(
        return_value={
            "id": "task_123456789abc",
            "name": "Revision-aware task",
            "entity_type": "task",
            "metadata": {"status": "doing"},
        }
    )

    result = CliRunner().invoke(
        task.app,
        ["start", "123456", "--expected-revision", "7"],
    )

    assert result.exit_code == 0
    mock_client.start_task.assert_awaited_once_with(
        "task_123456789abc",
        None,
        expected_revision=7,
    )
    assert "Revision: 8" in result.stdout
    assert "Operation: start-1 (applied)" in result.stdout


@patch("sibyl_cli.task.get_client")
def test_task_complete_json_preserves_api_policy_metadata(
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.complete_task = AsyncMock(
        return_value={
            "success": False,
            "message": "task learning capture denied: unverified_membership",
            "data": {"policy_reason": "unverified_membership"},
        }
    )
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(
        task.app,
        [
            "complete",
            "task_123456789abc",
            "--learnings",
            "Denied lesson",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["policy_reason"] == "unverified_membership"


@patch("sibyl_cli.task.get_client")
def test_task_archive_stdin_json_reports_failed_ids(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.resolve_id_prefix = AsyncMock(
        side_effect=SibylClientError(
            "No task ID matches prefix: not-a-task",
            status_code=404,
            detail="No task ID matches prefix: not-a-task",
        )
    )
    mock_client.archive_task = AsyncMock(
        side_effect=[
            {"success": True},
            {"success": False, "message": "already archived"},
        ]
    )
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(
        task.app,
        ["archive", "--stdin", "--yes", "--json"],
        input="task_123456789abc\nnot-a-task\n13364346-8475-4664-8b52-eb963af2fda7\n",
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["total"] == 3
    assert payload["archived"] == 1
    assert payload["failed"] == 2
    assert payload["failed_ids"] == ["not-a-task", "13364346-8475-4664-8b52-eb963af2fda7"]
    assert [item["id"] for item in payload["results"]] == [
        "task_123456789abc",
        "not-a-task",
        "13364346-8475-4664-8b52-eb963af2fda7",
    ]
    mock_client.archive_task.assert_any_await("task_123456789abc", None)
    mock_client.archive_task.assert_any_await("13364346-8475-4664-8b52-eb963af2fda7", None)
    assert mock_client.archive_task.await_count == 2


@patch("sibyl_cli.task.get_client")
def test_task_archive_stdin_accepts_uuid_ids(mock_get_client: MagicMock) -> None:
    task_id = "13364346-8475-4664-8b52-eb963af2fda7"
    mock_client = MagicMock()
    mock_client.archive_task = AsyncMock(return_value={"success": True})
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(
        task.app,
        ["archive", "--stdin", "--yes", "--json"],
        input=f"{task_id}\ntask_123456789abc\n",
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["archived"] == 2
    assert payload["failed_ids"] == []
    mock_client.archive_task.assert_any_await(task_id, None)
    mock_client.archive_task.assert_any_await("task_123456789abc", None)
