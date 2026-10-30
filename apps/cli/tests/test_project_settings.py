"""`sibyl project update --default-scope` and the default shown on `project show`."""

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli.client import SibylClientError
from sibyl_cli.main import app


def _client(**methods: AsyncMock) -> MagicMock:
    client = MagicMock()
    for name, method in methods.items():
        setattr(client, name, method)
    return client


@patch("sibyl_cli.project.resolve_project_reference", new_callable=AsyncMock)
@patch("sibyl_cli.project.get_client")
def test_project_update_shares_new_captures_with_the_project(get_client, resolve) -> None:
    resolve.return_value = "project_oncall"
    update = AsyncMock(
        return_value={
            "project_id": "project_oncall",
            "default_memory_scope": "project",
            "can_manage": True,
        }
    )
    get_client.return_value = _client(update_project_settings=update)

    result = CliRunner().invoke(app, ["project", "update", "oncall", "--default-scope", "project"])

    assert result.exit_code == 0, result.output
    update.assert_awaited_once_with("project_oncall", default_memory_scope="project")
    assert "Default write scope: project" in result.output
    assert "contributor" in result.output


@patch("sibyl_cli.project.get_client")
def test_project_update_rejects_scopes_a_project_cannot_default_to(get_client) -> None:
    result = CliRunner().invoke(
        app, ["project", "update", "project_oncall", "--default-scope", "team"]
    )

    assert result.exit_code == 1
    assert "private or project" in result.output
    get_client.assert_not_called()


@patch("sibyl_cli.project.get_client")
def test_project_show_prints_the_default_write_scope(get_client) -> None:
    get_client.return_value = _client(
        get_entity=AsyncMock(
            return_value={"id": "project_oncall", "name": "Oncall", "metadata": {}}
        ),
        get_project_settings=AsyncMock(
            return_value={"project_id": "project_oncall", "default_memory_scope": "project"}
        ),
    )

    result = CliRunner().invoke(app, ["project", "show", "project_oncall"])

    assert result.exit_code == 0, result.output
    assert "Default write scope" in result.output
    assert "shared with the project" in result.output


@patch("sibyl_cli.project.get_client")
def test_project_show_omits_the_default_on_servers_without_settings(get_client) -> None:
    get_client.return_value = _client(
        get_entity=AsyncMock(
            return_value={"id": "project_oncall", "name": "Oncall", "metadata": {}}
        ),
        get_project_settings=AsyncMock(side_effect=SibylClientError("not found", status_code=404)),
    )

    result = CliRunner().invoke(app, ["project", "show", "project_oncall"])

    assert result.exit_code == 0, result.output
    assert "Default write scope" not in result.output
