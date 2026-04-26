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
        sync=False,
    )
    assert "Queued episode" in result.stdout


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
        sync=False,
    )
    assert "Queued pattern" in result.stdout


@patch("sibyl_cli.main.get_client")
def test_add_command_waits_for_direct_readiness(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "pattern_123"})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["add", "Waitable Pattern", "Pattern body", "--type", "pattern", "--wait-searchable"],
    )

    assert result.exit_code == 0
    mock_client.create_entity.assert_awaited_once_with(
        name="Waitable Pattern",
        content="Pattern body",
        entity_type="pattern",
        category=None,
        languages=None,
        tags=None,
        sync=True,
    )
    mock_client.search.assert_not_called()
    assert "Added pattern" in result.stdout


@patch("sibyl_cli.main.get_client")
def test_capture_command_waits_for_direct_readiness(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "episode_123"})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "capture",
            "Longer memory body",
            "--title",
            "Manual title",
            "--wait-searchable",
        ],
    )

    assert result.exit_code == 0
    mock_client.create_entity.assert_awaited_once_with(
        name="Manual title",
        content="Longer memory body",
        entity_type="episode",
        tags=None,
        metadata={"capture_mode": "quick", "capture_surface": "cli"},
        sync=True,
    )
    mock_client.search.assert_not_called()
    assert "Captured episode" in result.stdout


@patch("sibyl_cli.main.get_client")
def test_remember_command_records_domain_memory_with_links(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "decision_123"})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "remember",
            "Use context packs",
            "Agents should receive grouped memory before building.",
            "--kind",
            "decision",
            "--domain",
            "agent-memory",
            "--tags",
            "agents,context",
            "--related-to",
            "plan_1,idea_2",
        ],
    )

    assert result.exit_code == 0
    mock_client.create_entity.assert_awaited_once_with(
        name="Use context packs",
        content="Agents should receive grouped memory before building.",
        entity_type="decision",
        category="agent-memory",
        tags=["agents", "context"],
        related_to=["plan_1", "idea_2"],
        metadata={
            "capture_mode": "remember",
            "capture_surface": "cli",
            "remember_kind": "decision",
            "domain": "agent-memory",
        },
        sync=False,
    )
    assert "Queued decision" in result.stdout


@patch("sibyl_cli.main.get_client")
def test_remember_command_reads_body_from_stdin(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "idea_123"})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["remember", "Model any domain", "--kind", "idea"], input="Body")

    assert result.exit_code == 0
    mock_client.create_entity.assert_awaited_once_with(
        name="Model any domain",
        content="Body",
        entity_type="idea",
        category=None,
        tags=None,
        related_to=None,
        metadata={
            "capture_mode": "remember",
            "capture_surface": "cli",
            "remember_kind": "idea",
        },
        sync=False,
    )


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_recall_command_outputs_markdown_context(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.context_pack = AsyncMock(
        return_value={
            "goal": "ship faster",
            "markdown": "# Sibyl Context Pack: ship faster\n\n## Decisions",
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["recall", "ship faster", "--intent", "plan"])

    assert result.exit_code == 0
    assert "# Sibyl Context Pack: ship faster" in result.stdout
    mock_client.context_pack.assert_awaited_once_with(
        goal="ship faster",
        intent="plan",
        domain=None,
        project="project_123",
        limit=12,
        include_related=True,
        related_limit=3,
    )
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_reflect_command_outputs_markdown_candidates(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.reflect = AsyncMock(
        return_value={
            "source_title": "Planning",
            "markdown": "# Sibyl Reflection: Planning\n\n## Decision: Use reflect",
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reflect",
            "We decided to build reflect.",
            "--title",
            "Planning",
            "--intent",
            "build",
            "--domain",
            "sibyl",
            "--related-to",
            "project_123",
            "--persist",
        ],
    )

    assert result.exit_code == 0
    assert "# Sibyl Reflection: Planning" in result.stdout
    mock_client.reflect.assert_awaited_once_with(
        content="We decided to build reflect.",
        source_title="Planning",
        intent="build",
        domain="sibyl",
        project="project_123",
        related_to=["project_123"],
        persist=True,
        persist_source=True,
        limit=12,
    )
    mock_resolve_project_from_cwd.assert_called_once_with()
