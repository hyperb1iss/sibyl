"""Root recall commands scope to one project unless --all asks for every project."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli.config_store import Context
from sibyl_cli.main import app


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


DEFAULT_CONTEXT = Context(
    name="local",
    server_url="http://localhost:3334",
    org_slug=None,
    default_project="project_default",
)
PACK = {
    "goal": "goal",
    "intent": "build",
    "query": "goal",
    "project": "project_default",
    "scope": "project",
    "sections": [],
    "total_items": 0,
    "markdown": "# Sibyl Context Pack: goal",
}


def _client() -> MagicMock:
    client = MagicMock()
    client.context_pack = AsyncMock(return_value=PACK)
    client.search = AsyncMock(return_value={"results": [], "total": 0})
    client.recall_raw_memory = AsyncMock(return_value={"memories": []})
    return client


def _runner() -> CliRunner:
    # Click 8.2+ keeps stdout and stderr apart; output is the combined stream.
    return CliRunner()


# (argv, client method that must never run when nothing names a project)
UNLINKED_ENTRY_POINTS = [
    (["context", "goal"], "context_pack"),
    (["recall", "goal"], "context_pack"),
    (["search", "goal"], "context_pack"),
    (["brief", "goal"], "context_pack"),
    (["graph-search", "goal"], "search"),
]


@pytest.mark.parametrize(("argv", "method"), UNLINKED_ENTRY_POINTS)
@patch("sibyl_cli.recall.resolve_effective_context", return_value=None)
@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value=None)
@patch("sibyl_cli.recall.get_client")
def test_unlinked_recall_refuses_instead_of_reading_every_project(
    mock_get_client: MagicMock,
    _mock_cwd: MagicMock,
    _mock_context: MagicMock,
    argv: list[str],
    method: str,
) -> None:
    client = _client()
    mock_get_client.return_value = _FakeClientContext(client)

    result = _runner().invoke(app, argv)

    assert result.exit_code == 1, result.output
    assert "sibyl project list" in result.output
    assert "--all" in result.output
    getattr(client, method).assert_not_called()


@pytest.mark.parametrize(("argv", "method"), UNLINKED_ENTRY_POINTS)
@patch("sibyl_cli.recall.resolve_effective_context", return_value=None)
@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value=None)
@patch("sibyl_cli.recall.get_client")
def test_all_reads_every_project_on_purpose(
    mock_get_client: MagicMock,
    _mock_cwd: MagicMock,
    _mock_context: MagicMock,
    argv: list[str],
    method: str,
) -> None:
    client = _client()
    mock_get_client.return_value = _FakeClientContext(client)

    result = _runner().invoke(app, [*argv, "--all"])

    assert result.exit_code == 0, result.output
    assert getattr(client, method).await_args.kwargs["project"] is None


@patch("sibyl_cli.recall.resolve_effective_context", return_value=DEFAULT_CONTEXT)
@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value=None)
@patch("sibyl_cli.recall.get_client")
def test_context_json_stays_parseable_under_the_default_project(
    mock_get_client: MagicMock, *_mocks: MagicMock
) -> None:
    client = _client()
    mock_get_client.return_value = _FakeClientContext(client)

    result = _runner().invoke(app, ["context", "goal", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["project"] == "project_default"
    assert client.context_pack.await_args.kwargs["project"] == "project_default"


@patch("sibyl_cli.recall.resolve_effective_context", return_value=DEFAULT_CONTEXT)
@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value=None)
@patch("sibyl_cli.recall.get_client")
def test_brief_stdout_is_only_the_pack_under_the_default_project(
    mock_get_client: MagicMock, *_mocks: MagicMock
) -> None:
    client = _client()
    mock_get_client.return_value = _FakeClientContext(client)

    result = _runner().invoke(app, ["brief", "goal"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "# Sibyl Context Pack: goal\n"
    assert "using the context default project_default" in result.stderr


@patch("sibyl_cli.project_scope.get_default_project", return_value="project_legacy")
@patch("sibyl_cli.recall.resolve_effective_context", return_value=None)
@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value=None)
@patch("sibyl_cli.recall.get_client")
def test_recall_honours_the_legacy_default_project(
    mock_get_client: MagicMock, *_mocks: MagicMock
) -> None:
    client = _client()
    mock_get_client.return_value = _FakeClientContext(client)

    result = _runner().invoke(app, ["context", "goal", "--json"])

    assert result.exit_code == 0, result.output
    assert client.context_pack.await_args.kwargs["project"] == "project_legacy"
    assert "using the configured default project_legacy" in result.stderr
