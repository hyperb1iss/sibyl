"""Tests for the root-level capture command."""

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli.main import _derive_capture_title, app


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


def test_derive_capture_title_truncates_cleanly() -> None:
    title = _derive_capture_title("  shipped   a fix for the archived paging bug " * 3)

    assert "  " not in title
    assert len(title) <= 72
    assert title.endswith("…")


@patch("sibyl_cli.main.get_client")
def test_capture_command_derives_title_and_marks_quick_capture(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "entity_123"})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["capture", "Shipped the link-graph fix after tracing org scope."])

    assert result.exit_code == 0
    mock_client.create_entity.assert_awaited_once_with(
        name="Shipped the link-graph fix after tracing org scope.",
        content="Shipped the link-graph fix after tracing org scope.",
        entity_type="episode",
        tags=None,
        metadata={"capture_mode": "quick", "capture_surface": "cli"},
    )


@patch("sibyl_cli.main.get_client")
def test_capture_command_title_override_wins(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "entity_456"})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["capture", "Longer memory body", "--title", "Manual title", "--type", "pattern"],
    )

    assert result.exit_code == 0
    mock_client.create_entity.assert_awaited_once_with(
        name="Manual title",
        content="Longer memory body",
        entity_type="pattern",
        tags=None,
        metadata={"capture_mode": "quick", "capture_surface": "cli"},
    )
