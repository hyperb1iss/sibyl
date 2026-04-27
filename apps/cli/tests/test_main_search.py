"""Tests for root-level search command rendering."""

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli.main import SEARCH_PREVIEW_CHARS, _format_search_preview, app


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


def test_format_search_preview_keeps_more_context() -> None:
    content = "[Docs > Search] " + ("alpha " * 18) + "MAGICLATE " + ("omega " * 20)

    preview = _format_search_preview(content)

    assert "MAGICLATE" in preview
    assert "[Docs > Search]" not in preview
    assert len(preview) > 100
    assert len(preview) <= SEARCH_PREVIEW_CHARS + 1
    assert preview.endswith("…")


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_search_command_renders_longer_previews(
    mock_get_client: MagicMock, mock_resolve_project_from_cwd: MagicMock
) -> None:
    mock_client = MagicMock()
    mock_client.search = AsyncMock(
        return_value={
            "results": [
                {
                    "id": "entity_123",
                    "name": "Result name",
                    "source": "example-source",
                    "content": "[Docs > Search] " + ("alpha " * 18) + "MAGICLATE " + ("omega " * 20),
                    "metadata": {"heading_path": ["Docs", "Search"]},
                }
            ]
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["search", "boop"])

    assert result.exit_code == 0
    assert "MAGICLATE" in result.stdout
    assert "[Docs > Search]" not in result.stdout
    mock_client.search.assert_called_once_with(
        "boop",
        types=None,
        limit=10,
        project="project_123",
    )
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.console.print")
@patch("sibyl_cli.main.get_client")
def test_search_command_soft_wraps_previews(
    mock_get_client: MagicMock,
    mock_console_print: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.search = AsyncMock(
        return_value={
            "results": [
                {
                    "id": "entity_123",
                    "name": "Result name",
                    "source": "example-source",
                    "content": "alpha " * 40,
                    "metadata": {},
                }
            ]
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["search", "boop"])

    assert result.exit_code == 0
    assert any(call.kwargs.get("soft_wrap") is True for call in mock_console_print.call_args_list)
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_recall_command_can_render_raw_memories(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.recall_raw_memory = AsyncMock(
        return_value={
            "query": "context packs",
            "memories": [
                {
                    "id": "memory_123",
                    "title": "Context packs",
                    "source_id": "cli:test",
                    "memory_scope": "private",
                    "score": 1.0,
                    "raw_content": "Context packs should carry source ids.",
                }
            ],
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["recall", "context packs", "--raw", "--limit", "5"])

    assert result.exit_code == 0
    mock_client.recall_raw_memory.assert_awaited_once_with(
        query="context packs",
        memory_scope="private",
        scope_key=None,
        limit=5,
    )
    assert "Context packs" in result.stdout
    assert "memory_123" in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()
