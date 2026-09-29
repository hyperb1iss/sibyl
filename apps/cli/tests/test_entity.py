"""Tests for entity CLI commands."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli.entity import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _flat(text: str) -> str:
    """Plain text with whitespace collapsed, so colour and wrapping cannot split a match."""
    return " ".join(_ANSI_RE.sub("", text).split())


@patch("sibyl_cli.entity.get_client")
def test_entity_show_renders_full_content(mock_get_client: MagicMock) -> None:
    content = "alpha " * 120 + "TAIL_SENTINEL"
    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(
        return_value={
            "id": "episode_123456789abc",
            "name": "Long memory",
            "entity_type": "episode",
            "description": "Short summary",
            "content": content,
            "metadata": {},
        }
    )
    mock_get_client.return_value = mock_client

    result = CliRunner().invoke(app, ["show", "episode_123456789abc"])

    assert result.exit_code == 0
    assert "TAIL_SENTINEL" in result.stdout
    assert "..." not in result.stdout
    mock_client.get_entity.assert_awaited_once_with("episode_123456789abc")


@patch("sibyl_cli.entity.get_client")
def test_entity_show_renders_raw_memory_reference(mock_get_client: MagicMock) -> None:
    content = "alpha " * 80 + "TAIL_SENTINEL"
    mock_client = MagicMock()
    mock_client.resolve_id_prefix = AsyncMock(return_value={"matches": [{"id": "memory-1"}]})
    mock_client.memory_inspect = AsyncMock(
        return_value={
            "id": "memory-1",
            "source_id": "source-1",
            "title": "Raw source",
            "content_redacted": False,
            "raw_content": content,
            "derived_ids": [],
            "audit_event_count": 0,
        }
    )
    mock_get_client.return_value = mock_client

    result = CliRunner().invoke(app, ["show", "raw_memory:memory-1"])

    assert result.exit_code == 0
    assert "Memory source" in result.stdout
    assert "TAIL_SENTINEL" in result.stdout
    assert "..." not in result.stdout
    mock_client.resolve_id_prefix.assert_awaited_once_with(
        "memory-1",
        entity_type="raw_memory",
    )
    mock_client.memory_inspect.assert_awaited_once_with("memory-1")


@patch("sibyl_cli.entity.get_client")
def test_entity_delete_refuses_a_raw_memory_before_sending_anything(
    mock_get_client: MagicMock,
) -> None:
    """A raw memory delete can never succeed on the graph route.

    Sent anyway, it failed with a 500 that the client buffered for replay, and
    every later command reported the stuck write until it was discarded by hand.
    """
    result = CliRunner().invoke(
        app, ["delete", "raw_memory:3f1c2a9e-0000-4000-8000-000000000000", "-y"]
    )

    output = _flat(result.stdout)
    assert result.exit_code == 1
    assert "is a raw memory, not a graph entity" in output
    assert (
        "sibyl correct 3f1c2a9e-0000-4000-8000-000000000000 --action delete "
        '--reason "<why>" --yes' in output
    )
    mock_get_client.assert_not_called()


@patch("sibyl_cli.entity.get_client")
def test_entity_delete_refuses_an_uppercase_raw_memory_prefix(mock_get_client: MagicMock) -> None:
    result = CliRunner().invoke(app, ["delete", "RAW_MEMORY:abc123", "-y"])

    output = _flat(result.stdout)
    assert result.exit_code == 1
    assert 'sibyl correct abc123 --action delete --reason "<why>" --yes' in output
    mock_get_client.assert_not_called()


@patch("sibyl_cli.entity.get_client")
def test_entity_delete_names_the_correct_command_when_a_bare_id_is_a_raw_memory(
    mock_get_client: MagicMock,
) -> None:
    """`sibyl remember` prints a raw memory's id bare, so a bare id reaches the route."""
    from sibyl_cli.client import SibylClientError

    raw_id = "21634470-46e1-4708-ad85-99c185e7bf65"
    mock_client = MagicMock()
    mock_client.delete_entity = AsyncMock(
        side_effect=SibylClientError("not found", status_code=404, error_code="not_found")
    )
    mock_client.memory_inspect = AsyncMock(return_value={"id": raw_id})
    mock_get_client.return_value = mock_client

    result = CliRunner().invoke(app, ["delete", raw_id, "-y"])

    output = _flat(result.stdout)
    assert result.exit_code == 1
    assert "is a raw memory, not a graph entity" in output
    assert f'sibyl correct {raw_id} --action delete --reason "<why>" --yes' in output
    mock_client.memory_inspect.assert_awaited_once_with(raw_id)


@patch("sibyl_cli.entity.get_client")
def test_entity_delete_reports_a_plain_miss_when_the_id_is_not_a_raw_memory(
    mock_get_client: MagicMock,
) -> None:
    from sibyl_cli.client import SibylClientError

    mock_client = MagicMock()
    mock_client.delete_entity = AsyncMock(
        side_effect=SibylClientError("Entity not found", status_code=404, error_code="not_found")
    )
    mock_client.memory_inspect = AsyncMock(
        side_effect=SibylClientError("not found", status_code=404)
    )
    mock_get_client.return_value = mock_client

    result = CliRunner().invoke(app, ["delete", "note_missing", "-y"])

    assert result.exit_code != 0
    assert "is a raw memory" not in _flat(result.stdout)
