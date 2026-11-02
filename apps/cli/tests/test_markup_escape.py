"""Stored text with square brackets prints verbatim instead of parsing as Rich markup.

A memory that mentions ``[/preview]`` used to crash every command that printed
it with ``rich.errors.MarkupError``, and an opening tag like ``[bold]`` was
silently swallowed. These tests feed bracket text through the recall paths and
the views that interpolate server text into our own markup.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from sibyl_cli import task
from sibyl_cli.client import SibylClientError
from sibyl_cli.common import console, handle_client_error
from sibyl_cli.config_store import Context
from sibyl_cli.main import app
from sibyl_cli.memory_display import print_memory_source_inspect

# A closing tag with no opener raises MarkupError; the others vanish silently.
BRACKETS = "see [/preview] and [bold]x[/bold] plus [docs](https://example.test)"


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


def _pack() -> dict:
    return {
        "goal": f"goal {BRACKETS}",
        "intent": "build",
        "layer": "recall",
        "domain": f"domain {BRACKETS}",
        "project": "project_default",
        "scope": "project",
        "sections": [
            {
                "title": f"Section {BRACKETS}",
                "items": [
                    {
                        "id": "decision_1",
                        "type": "decision",
                        "name": f"Name {BRACKETS}",
                        "reason": f"Reason {BRACKETS}",
                        "content": f"Content {BRACKETS}",
                        "related": [
                            {
                                "id": "pattern_1",
                                "relationship": "RELATED_TO",
                                "name": f"Related {BRACKETS}",
                            }
                        ],
                    }
                ],
            }
        ],
        "total_items": 1,
        "usage_hint": f"Hint {BRACKETS}",
        "markdown": f"# Sibyl Context Pack\n\n- **Memory** {BRACKETS}\n",
    }


@patch("sibyl_cli.recall.resolve_effective_context")
@patch("sibyl_cli.recall.resolve_project_from_cwd", return_value="project_default")
@patch("sibyl_cli.recall.get_client")
def test_root_context_prints_bracket_text_verbatim(
    mock_get_client: MagicMock,
    _mock_cwd: MagicMock,
    mock_context: MagicMock,
) -> None:
    mock_context.return_value = Context(
        name="local",
        server_url="http://localhost:3334",
        org_slug=None,
        default_project="project_default",
    )
    client = MagicMock()
    client.context_pack = AsyncMock(return_value=_pack())
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(app, ["context", "goal", "--intent", "build"])

    assert result.exception is None, result.output
    assert result.exit_code == 0, result.output
    assert f"- **Memory** {BRACKETS}" in result.stdout


@patch("sibyl_cli.context.resolve_project_from_cwd", return_value="project_default")
@patch("sibyl_cli.context.get_client")
def test_context_pack_markdown_prints_bracket_text_verbatim(
    mock_get_client: MagicMock,
    _mock_cwd: MagicMock,
) -> None:
    client = MagicMock()
    client.context_pack = AsyncMock(return_value=_pack())
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(app, ["config", "context", "pack", "goal", "--markdown"])

    assert result.exception is None, result.output
    assert f"- **Memory** {BRACKETS}" in result.stdout


@patch("sibyl_cli.context.resolve_project_from_cwd", return_value="project_default")
@patch("sibyl_cli.context.get_client")
def test_context_pack_view_escapes_every_stored_field(
    mock_get_client: MagicMock,
    _mock_cwd: MagicMock,
) -> None:
    client = MagicMock()
    client.context_pack = AsyncMock(return_value=_pack())
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(app, ["config", "context", "pack", "goal"])

    assert result.exception is None, result.output
    for label in ("goal", "domain", "Section", "Name", "Reason", "Content", "Related", "Hint"):
        assert f"{label} {BRACKETS}" in result.stdout


@patch("sibyl_cli.memory.get_client")
def test_reflect_prints_bracket_text_verbatim(mock_get_client: MagicMock) -> None:
    client = MagicMock()
    client.reflect = AsyncMock(return_value={"markdown": f"## Candidates\n{BRACKETS}\n"})
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(app, ["reflect", "session notes", "--project", "project_1"])

    assert result.exception is None, result.output
    assert BRACKETS in result.stdout


def _task_client() -> MagicMock:
    client = MagicMock()
    client.resolve_id_prefix = AsyncMock(return_value={"matches": [{"id": "task_123456789abc"}]})
    client.get_entity = AsyncMock(
        return_value={
            "id": "task_123456789abc",
            "name": f"Title {BRACKETS}",
            "description": f"Description {BRACKETS}",
            "entity_type": "task",
            "metadata": {
                "status": "doing",
                "priority": "high",
                "feature": "feature [/x]",
                "branch_name": "branch [/y]",
            },
        }
    )
    client.list_notes = AsyncMock(
        return_value={
            "notes": [
                {
                    "author_type": "agent",
                    "author_name": "nova [/z]",
                    "created_at": "2026-11-02T10:00:00",
                    "content": f"Note {BRACKETS}",
                }
            ]
        }
    )
    return client


@patch("sibyl_cli.task.get_client")
def test_task_show_escapes_stored_text(mock_get_client: MagicMock) -> None:
    mock_get_client.return_value = _task_client()

    result = CliRunner().invoke(task.app, ["show", "task_123456789abc"])

    assert result.exception is None, result.output
    # The panel wraps long lines, so check each stored fragment on its own.
    for fragment in ("[/preview]", "[bold]x[/bold]", "feature [/x]", "branch [/y]"):
        assert fragment in result.stdout


@patch("sibyl_cli.task.get_client")
def test_task_notes_escape_author_and_content(mock_get_client: MagicMock) -> None:
    mock_get_client.return_value = _task_client()

    result = CliRunner().invoke(task.app, ["notes", "task_123456789abc"])

    assert result.exception is None, result.output
    assert "nova [/z]" in result.stdout
    assert f"Note {BRACKETS}" in result.stdout


def test_client_error_detail_prints_verbatim() -> None:
    err = SibylClientError("rejected", status_code=400, detail=f"bad input {BRACKETS}")

    with console.capture() as captured, pytest.raises(typer.Exit):
        handle_client_error(err)

    assert f"bad input {BRACKETS}" in captured.get()


def test_raw_memory_inspect_prints_content_verbatim() -> None:
    with console.capture() as captured:
        print_memory_source_inspect(
            {
                "id": "raw_memory_1",
                "title": "Title [/t]",
                "raw_content": f"Content {BRACKETS}",
            },
            full_content=True,
        )

    output = captured.get()
    assert "Title [/t]" in output
    assert f"Content {BRACKETS}" in output
