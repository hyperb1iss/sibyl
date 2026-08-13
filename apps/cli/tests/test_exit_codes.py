"""Exit-code contract for CLI failure paths.

Hooks, CI jobs, and agents branch on `$?`. A refused write, an expired
session, and an unreachable server must all exit nonzero; an empty result
set is a successful answer and must not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli import entity, epic, project, task
from sibyl_cli.client import SibylClientError
from sibyl_cli.main import app as main_app

TASK_ID = "13364346-8475-4664-8b52-eb963af2fda7"
REFUSED: dict[str, Any] = {"success": False, "message": "Write refused"}


def _client(**methods: Any) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    for name, value in methods.items():
        setattr(client, name, AsyncMock(return_value=value))
    return client


@pytest.fixture
def resolved_ids():
    with (
        patch("sibyl_cli.task._resolve_task_id", AsyncMock(side_effect=lambda _c, i: i)),
        patch("sibyl_cli.epic._resolve_epic_id", AsyncMock(side_effect=lambda _c, i: i)),
    ):
        yield


def test_task_start_exits_nonzero_when_the_write_is_refused(resolved_ids: None) -> None:
    with patch("sibyl_cli.task.get_client", return_value=_client(start_task=REFUSED)):
        result = CliRunner().invoke(task.app, ["start", TASK_ID])

    assert result.exit_code == 1


def test_task_complete_exits_nonzero_when_the_write_is_refused(resolved_ids: None) -> None:
    with patch("sibyl_cli.task.get_client", return_value=_client(complete_task=REFUSED)):
        result = CliRunner().invoke(task.app, ["complete", TASK_ID])

    assert result.exit_code == 1


def test_task_update_exits_nonzero_when_the_write_is_refused(resolved_ids: None) -> None:
    with patch("sibyl_cli.task.get_client", return_value=_client(update_task=REFUSED)):
        result = CliRunner().invoke(task.app, ["update", TASK_ID, "--priority", "high"])

    assert result.exit_code == 1


def test_task_create_exits_nonzero_when_the_write_is_refused() -> None:
    with patch("sibyl_cli.task.get_client", return_value=_client(create_task=REFUSED)):
        result = CliRunner().invoke(
            task.app,
            ["create", "--title", "Refused", "--project", "project_123456789abc"],
        )

    assert result.exit_code == 1


def test_epic_start_exits_nonzero_when_the_write_is_refused(resolved_ids: None) -> None:
    with patch("sibyl_cli.epic.get_client", return_value=_client(update_entity=REFUSED)):
        result = CliRunner().invoke(epic.app, ["start", "epic_123"])

    assert result.exit_code == 1


def test_entity_create_exits_nonzero_when_the_write_is_refused() -> None:
    with patch("sibyl_cli.entity.get_client", return_value=_client(create_entity={})):
        result = CliRunner().invoke(entity.app, ["create", "--name", "Refused", "--type", "note"])

    assert result.exit_code == 1


def test_project_create_exits_nonzero_when_the_write_is_refused() -> None:
    with patch("sibyl_cli.project.get_client", return_value=_client(create_entity={})):
        result = CliRunner().invoke(project.app, ["create", "--name", "Refused"])

    assert result.exit_code == 1


def test_task_start_exits_nonzero_on_an_expired_session(resolved_ids: None) -> None:
    client = _client()
    client.start_task = AsyncMock(
        side_effect=SibylClientError("Not authenticated", status_code=401)
    )

    with patch("sibyl_cli.task.get_client", return_value=client):
        result = CliRunner().invoke(task.app, ["start", TASK_ID])

    assert result.exit_code == 1


def test_task_start_exits_nonzero_when_the_server_is_unreachable(resolved_ids: None) -> None:
    client = _client()
    client.start_task = AsyncMock(
        side_effect=SibylClientError("Cannot connect to Sibyl server at http://localhost:3334")
    )

    with patch("sibyl_cli.task.get_client", return_value=client):
        result = CliRunner().invoke(task.app, ["start", TASK_ID])

    assert result.exit_code == 1


@pytest.mark.parametrize("status_code", [403, 409, 500, 503])
def test_task_start_exits_nonzero_on_server_error_envelopes(
    resolved_ids: None,
    status_code: int,
) -> None:
    client = _client()
    client.start_task = AsyncMock(
        side_effect=SibylClientError("Request failed", status_code=status_code)
    )

    with patch("sibyl_cli.task.get_client", return_value=client):
        result = CliRunner().invoke(task.app, ["start", TASK_ID])

    assert result.exit_code == 1


def test_note_alias_exits_nonzero_when_the_write_is_refused() -> None:
    client = _client(create_note={})

    with (
        patch("sibyl_cli.main.get_client", return_value=client),
        patch("sibyl_cli.main.resolve_id_prefix", AsyncMock(side_effect=lambda _c, i, **_k: i)),
    ):
        result = CliRunner().invoke(main_app, ["note", TASK_ID, "body"])

    assert result.exit_code == 1


def test_local_stop_exits_nonzero_when_compose_fails(tmp_path: Path) -> None:
    from sibyl_cli import local

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n")
    failed = MagicMock()
    failed.returncode = 1

    with (
        patch.object(local, "SIBYL_LOCAL_COMPOSE", compose_file),
        patch("sibyl_cli.local.is_running", return_value=True),
        patch("sibyl_cli.local.run_compose", return_value=failed),
    ):
        result = CliRunner().invoke(local.app, ["stop"])

    assert result.exit_code == 1
    assert "Failed to stop Sibyl" in result.stdout


def test_an_empty_result_set_still_exits_zero() -> None:
    """No matches is an answer, not a failure. Scripts must not treat it as one."""
    client = _client(explore={"entities": [], "total": 0})

    with (
        patch("sibyl_cli.task.get_client", return_value=client),
        patch("sibyl_cli.task.resolve_project_from_cwd", return_value="project_123"),
    ):
        result = CliRunner().invoke(task.app, ["list"])

    assert result.exit_code == 0


def test_the_401_remediation_names_commands_that_exist() -> None:
    """The most-hit error path pointed at `sibyl auth signup`, which was never registered."""
    from sibyl_cli import auth

    client = _client()
    client.get = AsyncMock(side_effect=SibylClientError("Not authenticated", status_code=401))

    with patch("sibyl_cli.main.get_client", return_value=client):
        result = CliRunner().invoke(main_app, ["health"])

    assert result.exit_code == 1
    suggested = {
        line.split("sibyl auth ", 1)[1].split()[0]
        for line in result.stdout.splitlines()
        if "sibyl auth " in line
    }
    registered = {command.name for command in auth.app.registered_commands if command.name} | {
        group.name for group in auth.app.registered_groups if group.name
    }

    assert suggested
    assert suggested <= registered
