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


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_records_domain_memory_with_links(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "decision_123"})
    mock_client.explore = AsyncMock(return_value={"entities": []})
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
            "project_id": "project_123",
        },
        sync=False,
    )
    mock_client.explore.assert_awaited_once_with(
        mode="list",
        types=["task"],
        status="doing",
        project="project_123",
        limit=2,
    )
    assert "Queued decision" in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value=None)
@patch("sibyl_cli.main.get_client")
def test_remember_command_reads_body_from_stdin(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "idea_123"})
    mock_client.explore = AsyncMock()
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
    mock_client.explore.assert_not_called()
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_can_store_raw_memory(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.remember_raw_memory = AsyncMock(return_value={"id": "memory_123"})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "remember",
            "Raw Sibyl note",
            "Keep verbatim notes before reflection.",
            "--raw",
            "--source-id",
            "cli:test",
            "--tags",
            "raw,memory",
        ],
    )

    assert result.exit_code == 0
    mock_client.remember_raw_memory.assert_awaited_once_with(
        title="Raw Sibyl note",
        raw_content="Keep verbatim notes before reflection.",
        source_id="cli:test",
        memory_scope="private",
        scope_key=None,
        tags=["raw", "memory"],
        metadata={
            "capture_mode": "remember",
            "capture_surface": "cli",
            "remember_kind": "episode",
            "project_id": "project_123",
        },
        provenance={"remember_kind": "episode"},
        capture_surface="cli",
    )
    assert "Remembered raw memory" in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_from_path")
@patch("sibyl_cli.main.get_client")
def test_remember_command_project_option_overrides_path_context(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "plan_123"})
    mock_client.explore = AsyncMock(return_value={"entities": []})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "remember",
            "Build reusable memory",
            "Scope memory to project.",
            "--project",
            "project_explicit",
        ],
    )

    assert result.exit_code == 0
    mock_client.create_entity.assert_awaited_once()
    payload = mock_client.create_entity.await_args.kwargs
    assert payload["metadata"]["project_id"] == "project_explicit"
    mock_client.explore.assert_awaited_once_with(
        mode="list",
        types=["task"],
        status="doing",
        project="project_explicit",
        limit=2,
    )
    mock_resolve_project_from_cwd.assert_not_called()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_auto_links_single_active_project_task(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "decision_123"})
    mock_client.explore = AsyncMock(return_value={"entities": [{"id": "task_active"}]})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "remember",
            "Capture active work",
            "Memories should attach to the task agents are building.",
            "--kind",
            "decision",
        ],
    )

    assert result.exit_code == 0
    mock_client.create_entity.assert_awaited_once()
    payload = mock_client.create_entity.await_args.kwargs
    assert payload["related_to"] == ["task_active"]
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_skips_ambiguous_active_project_tasks(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "decision_123"})
    mock_client.explore = AsyncMock(
        return_value={"entities": [{"id": "task_one"}, {"id": "task_two"}]}
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "remember",
            "Capture active work",
            "Ambiguous active tasks should not receive automatic links.",
            "--related-to",
            "plan_1",
        ],
    )

    assert result.exit_code == 0
    payload = mock_client.create_entity.await_args.kwargs
    assert payload["related_to"] == ["plan_1"]
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_explicit_task_links_and_no_active_task(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.create_entity = AsyncMock(return_value={"id": "decision_123"})
    mock_client.explore = AsyncMock()
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "remember",
            "Capture active work",
            "Explicit task links should not require active-task lookup.",
            "--related-to",
            "plan_1",
            "--task",
            "task_1,plan_1",
            "--no-active-task",
        ],
    )

    assert result.exit_code == 0
    payload = mock_client.create_entity.await_args.kwargs
    assert payload["related_to"] == ["plan_1", "task_1"]
    mock_client.explore.assert_not_called()
    mock_resolve_project_from_cwd.assert_called_once_with()


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
            "source_id": "session_123",
            "persisted_count": 1,
            "total_candidates": 1,
            "candidates": [{"kind": "decision", "persisted_id": "decision_123"}],
            "markdown": "# Sibyl Reflection: Planning\n\n## Decision: Use reflect",
        }
    )
    mock_client.explore = AsyncMock(return_value={"entities": []})
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
    assert "Persisted source: session_123" in result.stdout
    assert "Persisted candidates: 1/1" in result.stdout
    assert "ID: decision_123" in result.stdout
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
    mock_client.explore.assert_awaited_once_with(
        mode="list",
        types=["task"],
        status="doing",
        project="project_123",
        limit=2,
    )
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_reflect_command_reads_notes_from_stdin(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.reflect = AsyncMock(
        return_value={
            "source_title": "Planning",
            "markdown": "# Sibyl Reflection: Planning\n\n## Plan: Build reflect",
        }
    )
    mock_client.explore = AsyncMock()
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["reflect", "--title", "Planning"], input="Build notes\n")

    assert result.exit_code == 0
    assert "# Sibyl Reflection: Planning" in result.stdout
    mock_client.reflect.assert_awaited_once_with(
        content="Build notes",
        source_title="Planning",
        intent="general",
        domain=None,
        project="project_123",
        related_to=None,
        persist=False,
        persist_source=True,
        limit=12,
    )
    mock_client.explore.assert_not_called()
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_reflect_command_can_persist_candidates_without_source(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.reflect = AsyncMock(
        return_value={
            "source_title": "Planning",
            "source_id": None,
            "persisted_count": 1,
            "total_candidates": 1,
            "candidates": [{"kind": "claim", "persisted_id": "claim_123"}],
            "markdown": "# Sibyl Reflection: Planning\n\n## Claim: Reflect works",
        }
    )
    mock_client.explore = AsyncMock(return_value={"entities": []})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reflect",
            "Reflect can write candidates only.",
            "--title",
            "Planning",
            "--persist",
            "--no-source",
        ],
    )

    assert result.exit_code == 0
    assert "Source persistence skipped (--no-source)" in result.stdout
    assert "Persisted candidates: 1/1" in result.stdout
    assert "ID: claim_123" in result.stdout
    mock_client.reflect.assert_awaited_once_with(
        content="Reflect can write candidates only.",
        source_title="Planning",
        intent="general",
        domain=None,
        project="project_123",
        related_to=None,
        persist=True,
        persist_source=False,
        limit=12,
    )
    mock_client.explore.assert_awaited_once_with(
        mode="list",
        types=["task"],
        status="doing",
        project="project_123",
        limit=2,
    )
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_reflect_command_persist_auto_links_single_active_project_task(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.reflect = AsyncMock(
        return_value={
            "source_title": "Planning",
            "source_id": "session_123",
            "persisted_count": 1,
            "total_candidates": 1,
            "candidates": [{"kind": "decision", "persisted_id": "decision_123"}],
            "markdown": "# Sibyl Reflection: Planning",
        }
    )
    mock_client.explore = AsyncMock(return_value={"entities": [{"id": "task_active"}]})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reflect",
            "We decided to link reflection output to active task context.",
            "--title",
            "Planning",
            "--persist",
        ],
    )

    assert result.exit_code == 0
    mock_client.reflect.assert_awaited_once()
    payload = mock_client.reflect.await_args.kwargs
    assert payload["related_to"] == ["task_active"]
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_reflect_command_explicit_task_links_and_no_active_task(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.reflect = AsyncMock(
        return_value={
            "source_title": "Planning",
            "source_id": "session_123",
            "persisted_count": 1,
            "total_candidates": 1,
            "candidates": [{"kind": "decision", "persisted_id": "decision_123"}],
            "markdown": "# Sibyl Reflection: Planning",
        }
    )
    mock_client.explore = AsyncMock()
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reflect",
            "Explicit task links should not require active-task lookup.",
            "--title",
            "Planning",
            "--persist",
            "--related-to",
            "plan_1",
            "--task",
            "task_1,plan_1",
            "--no-active-task",
        ],
    )

    assert result.exit_code == 0
    payload = mock_client.reflect.await_args.kwargs
    assert payload["related_to"] == ["plan_1", "task_1"]
    mock_client.explore.assert_not_called()
    mock_resolve_project_from_cwd.assert_called_once_with()
