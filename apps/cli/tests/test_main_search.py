"""Tests for root-level search command rendering."""

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli.main import app
from sibyl_cli.memory_views import (
    SEARCH_PREVIEW_CHARS,
    _format_search_preview,
    format_highlight_preview,
)


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


def test_format_search_preview_keeps_more_context() -> None:
    content = "[Docs > Search] " + ("alpha " * 45) + "MAGICLATE " + ("omega " * 45)

    preview = _format_search_preview(content)

    assert "MAGICLATE" in preview
    assert "[Docs > Search]" not in preview
    assert len(preview) > 300
    assert len(preview) <= SEARCH_PREVIEW_CHARS + 1
    assert preview.endswith("…")


def test_format_highlight_preview_renders_mark_tags_as_rich_markup() -> None:
    preview = format_highlight_preview("alpha <mark>beta</mark> gamma", "ignored")

    assert "<mark>" not in preview
    assert "</mark>" not in preview
    assert "[bold" in preview
    assert "beta" in preview


@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.recall.get_client")
def test_search_alias_returns_context_pack(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.context_pack = AsyncMock(return_value={"markdown": "# Agent context"})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(app, ["search", "ship context"])

    assert result.exit_code == 0
    assert "# Agent context" in result.stdout
    mock_client.context_pack.assert_awaited_once_with(
        goal="ship context",
        intent="build",
        layer="recall",
        domain=None,
        project="project_123",
        agent_id=None,
        limit=12,
        include_related=True,
        related_limit=3,
        audit=False,
        markdown_token_budget=None,
    )
    mock_client.search.assert_not_called()
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.recall.get_client")
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
                    "policy_reason": "private_principal_bound",
                    "score": 1.0,
                    "raw_content": "Context packs should carry source ids.",
                    "snippet": "Context packs should carry <mark>source ids</mark>.",
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
        diary=False,
        agent_id=None,
        project_id=None,
        limit=5,
    )
    assert "Context packs" in result.stdout
    assert "memory_123" in result.stdout
    assert "policy=private_principal_bound" in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.recall.get_client")
def test_recall_command_forwards_raw_metadata_filters(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.recall_raw_memory = AsyncMock(return_value={"memories": []})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "recall",
            "surrealdb",
            "--raw",
            "--participant",
            "nova@example.com",
            "--label",
            "email",
            "--thread",
            "thread-1",
            "--occurred-after",
            "2014-01-01T00:00:00+00:00",
            "--occurred-before",
            "2014-12-31T23:59:59+00:00",
            "--as-of",
            "2014-07-01T00:00:00+00:00",
        ],
    )

    assert result.exit_code == 0
    mock_client.recall_raw_memory.assert_awaited_once_with(
        query="surrealdb",
        memory_scope="private",
        scope_key=None,
        diary=False,
        agent_id=None,
        project_id=None,
        limit=12,
        participants=["nova@example.com"],
        labels=["email"],
        thread_id="thread-1",
        occurred_after="2014-01-01T00:00:00+00:00",
        occurred_before="2014-12-31T23:59:59+00:00",
        as_of="2014-07-01T00:00:00+00:00",
    )
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.recall.get_client")
def test_recall_command_can_render_agent_diary(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.recall_raw_memory = AsyncMock(
        return_value={
            "query": "implementation state",
            "memories": [
                {
                    "id": "memory_123",
                    "title": "Nova diary",
                    "source_id": "agent_diary:manual",
                    "memory_scope": "private",
                    "score": 1.0,
                    "raw_content": "Keep private implementation state.",
                }
            ],
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["recall", "implementation state", "--diary", "--agent", "nova", "--limit", "5"],
    )

    assert result.exit_code == 0
    mock_client.recall_raw_memory.assert_awaited_once_with(
        query="implementation state",
        memory_scope="private",
        scope_key=None,
        diary=True,
        agent_id="nova",
        project_id="project_123",
        limit=5,
    )
    assert "Nova diary" in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.recall.get_client")
def test_recall_command_diary_requires_agent(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.recall_raw_memory = AsyncMock(return_value={"memories": []})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["recall", "implementation state", "--diary"])

    assert result.exit_code == 1
    assert "Provide --agent" in result.stdout
    mock_client.recall_raw_memory.assert_not_awaited()
    mock_resolve_project_from_cwd.assert_called_once_with()
