"""Tests for entity CLI commands."""

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli.entity import app


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

    output = " ".join(result.stdout.split())
    assert result.exit_code == 1
    assert "is a raw memory, not a graph entity" in output
    assert (
        'sibyl correct 3f1c2a9e-0000-4000-8000-000000000000 --action delete --reason "<why>"'
        in output
    )
    mock_get_client.assert_not_called()
