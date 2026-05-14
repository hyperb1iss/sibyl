"""Tests for the root-level capture command."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli.client import SibylClient, SibylClientError
from sibyl_cli.main import _derive_capture_title, _parse_id_args, app


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


def test_parse_id_args_accepts_csv_and_positional_values() -> None:
    assert _parse_id_args(["raw-1,raw-2", "raw-2", "raw-3"]) == [
        "raw-1",
        "raw-2",
        "raw-3",
    ]


@pytest.mark.asyncio
async def test_memory_inspect_client_url_encodes_source_id() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="token")
    client._request = AsyncMock(return_value={"id": "memory-1"})  # type: ignore[method-assign]

    data = await client.memory_inspect("source/provenance:1")

    assert data == {"id": "memory-1"}
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "GET",
        "/memory/inspect/source%2Fprovenance%3A1",
    )


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
    mock_client.remember_raw_memory = AsyncMock(
        return_value={
            "id": "raw_123",
            "source_id": "cli:manual",
            "policy_reason": "private_principal_bound",
        }
    )
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
    mock_client.remember_raw_memory.assert_awaited_once_with(
        title="Use context packs",
        raw_content="Agents should receive grouped memory before building.",
        source_id=None,
        memory_scope="private",
        scope_key=None,
        diary=False,
        agent_id=None,
        project_id=None,
        tags=["agents", "context"],
        metadata={
            "capture_mode": "remember",
            "capture_surface": "cli",
            "remember_kind": "decision",
            "domain": "agent-memory",
            "project_id": "project_123",
        },
        provenance={
            "remember_kind": "decision",
            "related_to": ["plan_1", "idea_2"],
        },
        capture_surface="cli",
    )
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
            "raw_memory_id": "raw_123",
            "raw_source_id": "cli:manual",
            "raw_policy_reason": "private_principal_bound",
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
    assert "Policy: private_principal_bound" in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_writes_project_raw_source_before_graph_entity(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def remember_raw_memory(**kwargs: object) -> dict[str, str]:
        events.append(("raw", dict(kwargs)))
        return {"id": "raw_project_123", "source_id": "cli:manual"}

    async def create_entity(**kwargs: object) -> dict[str, str]:
        events.append(("graph", dict(kwargs)))
        return {"id": "decision_123"}

    mock_client = MagicMock()
    mock_client.remember_raw_memory = AsyncMock(side_effect=remember_raw_memory)
    mock_client.create_entity = AsyncMock(side_effect=create_entity)
    mock_client.explore = AsyncMock(return_value={"entities": []})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "remember",
            "Project raw source",
            "Preserve the exact source before graph summarization.",
            "--kind",
            "decision",
            "--scope",
            "project",
        ],
    )

    assert result.exit_code == 0
    assert [event_name for event_name, _ in events] == ["raw", "graph"]
    assert events[0][1]["memory_scope"] == "project"
    assert events[0][1]["scope_key"] == "project_123"
    assert events[1][1]["metadata"] == {
        "capture_mode": "remember",
        "capture_surface": "cli",
        "remember_kind": "decision",
        "project_id": "project_123",
        "raw_memory_id": "raw_project_123",
        "raw_source_id": "cli:manual",
    }
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value=None)
@patch("sibyl_cli.main.get_client")
def test_remember_command_reads_body_from_stdin(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.remember_raw_memory = AsyncMock(
        return_value={"id": "raw_123", "source_id": "cli:manual"}
    )
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
            "raw_memory_id": "raw_123",
            "raw_source_id": "cli:manual",
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
    mock_client.remember_raw_memory = AsyncMock(
        return_value={"id": "memory_123", "policy_reason": "private_principal_bound"}
    )
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
        diary=False,
        agent_id=None,
        project_id=None,
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
    assert "Policy: private_principal_bound" in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_can_store_agent_diary(
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
            "Nova diary",
            "Keep private implementation state.",
            "--diary",
            "--agent",
            "nova",
        ],
    )

    assert result.exit_code == 0
    mock_client.remember_raw_memory.assert_awaited_once_with(
        title="Nova diary",
        raw_content="Keep private implementation state.",
        source_id=None,
        memory_scope="private",
        scope_key=None,
        diary=True,
        agent_id="nova",
        project_id="project_123",
        tags=None,
        metadata={
            "capture_mode": "remember",
            "capture_surface": "cli",
            "remember_kind": "episode",
            "project_id": "project_123",
        },
        provenance={"remember_kind": "episode"},
        capture_surface="cli",
    )
    assert "Remembered diary entry for nova" in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_diary_requires_agent(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.remember_raw_memory = AsyncMock(return_value={"id": "memory_123"})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["remember", "Diary", "state", "--diary"])

    assert result.exit_code == 1
    assert "Provide --agent" in result.stdout
    mock_client.remember_raw_memory.assert_not_awaited()
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_from_path")
@patch("sibyl_cli.main.get_client")
def test_remember_command_project_option_overrides_path_context(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.remember_raw_memory = AsyncMock(
        return_value={"id": "raw_123", "source_id": "cli:manual"}
    )
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
    assert payload["metadata"]["raw_memory_id"] == "raw_123"
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
    mock_client.remember_raw_memory = AsyncMock(
        return_value={"id": "raw_123", "source_id": "cli:manual"}
    )
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
    assert payload["metadata"]["raw_memory_id"] == "raw_123"
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_skips_ambiguous_active_project_tasks(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.remember_raw_memory = AsyncMock(
        return_value={"id": "raw_123", "source_id": "cli:manual"}
    )
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
    assert payload["metadata"]["raw_memory_id"] == "raw_123"
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_remember_command_explicit_task_links_and_no_active_task(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.remember_raw_memory = AsyncMock(
        return_value={"id": "raw_123", "source_id": "cli:manual"}
    )
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
    assert payload["metadata"]["raw_memory_id"] == "raw_123"
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
        layer="recall",
        domain=None,
        project="project_123",
        agent_id=None,
        limit=12,
        include_related=True,
        related_limit=3,
    )
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_recall_command_reports_project_access_denied(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.context_pack = AsyncMock(
        side_effect=SibylClientError(
            "API error: Requires viewer access to project",
            status_code=403,
            detail="Requires viewer access to project project=project_123",
        )
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["recall", "ship faster"])

    assert result.exit_code == 1
    assert "Access denied" in result.stdout
    assert "Requires viewer access to project" in result.stdout
    assert "Authentication required" not in result.stdout
    assert "sibyl auth login" not in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.get_client")
def test_memory_audit_command_lists_events(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.memory_audit = AsyncMock(
        return_value={
            "events": [
                {
                    "created_at": "2026-05-13T12:00:00",
                    "action": "memory.remember",
                    "memory_scope": "project",
                    "scope_key": "project_123",
                    "source_ids": ["source-1", "source-2", "source-3"],
                    "source_ids_truncated": 2,
                    "derived_ids": ["memory-1"],
                    "policy_allowed": True,
                }
            ]
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "memory-audit",
            "--action",
            "memory.remember",
            "--source-id",
            "source-1",
            "--policy",
            "allowed",
            "--limit",
            "5",
        ],
    )

    assert result.exit_code == 0
    assert "memory.remember" in result.stdout
    assert "source" in result.stdout
    assert "+3" in result.stdout
    mock_client.memory_audit.assert_awaited_once_with(
        action="memory.remember",
        actor_user_id=None,
        source_id="source-1",
        derived_id=None,
        memory_scope=None,
        project_id=None,
        policy_allowed=True,
        limit=5,
    )


@patch("sibyl_cli.main.get_client")
def test_memory_inspect_command_renders_source_summary(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.memory_inspect = AsyncMock(
        return_value={
            "id": "memory-1",
            "source_id": "source-1",
            "title": "Raw source",
            "memory_scope": "project",
            "scope_key": "project_123",
            "project_id": "project_123",
            "review_state": "promoted",
            "entity_type": "procedure",
            "policy_allowed": False,
            "policy_reason": "unverified_membership",
            "content_redacted": True,
            "derived_ids": ["entity-1", "entity-2", "entity-3"],
            "audit_event_count": 2,
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["memory-inspect", "memory-1"])

    assert result.exit_code == 0
    assert "Memory source" in result.stdout
    assert "memory-1" in result.stdout
    assert "source-1" in result.stdout
    assert "redacted" in result.stdout
    assert "+1" in result.stdout
    mock_client.memory_inspect.assert_awaited_once_with("memory-1")


@patch("sibyl_cli.main.get_client")
def test_memory_inspect_command_outputs_json(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.memory_inspect = AsyncMock(
        return_value={
            "id": "memory-1",
            "source_id": "source-1",
            "content_redacted": False,
            "raw_content": "visible content",
            "derived_ids": [],
            "audit_event_count": 0,
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["memory-inspect", "memory-1", "--json"])

    assert result.exit_code == 0
    assert '"id": "memory-1"' in result.stdout
    assert '"raw_content": "visible content"' in result.stdout
    mock_client.memory_inspect.assert_awaited_once_with("memory-1")


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_memory_promote_preview_command_renders_decision(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.preview_reflection_promotion = AsyncMock(
        return_value={
            "allowed": False,
            "candidate_id": "candidate-1",
            "reason": "scope_crossing_requires_promotion",
            "review_state": "pending",
            "promote_to_scope": "project",
            "promote_to_scope_key": "project_123",
            "raw_source_ids": ["source-1"],
            "policy_reasons": ["scope_crossing_requires_promotion"],
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "memory-promote",
            "candidate-1",
            "--preview",
            "--scope",
            "project",
            "--domain",
            "sibyl",
            "--related-to",
            "plan_1",
            "--task",
            "task_1",
        ],
    )

    assert result.exit_code == 0
    assert "Promotion preview" in result.stdout
    assert "denied" in result.stdout
    assert "candidate-1" in result.stdout
    assert "project:project_123" in result.stdout
    mock_client.preview_reflection_promotion.assert_awaited_once_with(
        candidate_id="candidate-1",
        promote_to_scope="project",
        promote_to_scope_key="project_123",
        domain="sibyl",
        project="project_123",
        related_to=["plan_1", "task_1"],
    )
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.get_client")
def test_memory_promote_without_preview_is_denied(mock_get_client: MagicMock) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["memory-promote", "candidate-1"])

    assert result.exit_code == 1
    assert "only supports --preview" in result.stdout
    mock_get_client.assert_not_called()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_memory_share_preview_command_renders_redactions(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.preview_memory_share = AsyncMock(
        return_value={
            "allowed": False,
            "reason": "scope_not_enabled",
            "target_scope": "project",
            "target_scope_key": "project_123",
            "source_ids": ["raw-1", "raw-2"],
            "visible_source_ids": ["raw-1"],
            "denied_source_ids": ["raw-2"],
            "missing_source_ids": ["raw-2"],
            "redacted_count": 1,
            "hidden_but_relevant_count": 1,
            "policy_reasons": ["scope_not_enabled", "source_not_found"],
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "memory-share",
            "raw-1,raw-2",
            "--preview",
            "--target-scope",
            "project",
        ],
    )

    assert result.exit_code == 0
    assert "Share preview" in result.stdout
    assert "denied" in result.stdout
    assert "raw-1" in result.stdout
    assert "raw-2" in result.stdout
    assert "source_not_found" in result.stdout
    mock_client.preview_memory_share.assert_awaited_once_with(
        source_ids=["raw-1", "raw-2"],
        target_scope="project",
        target_scope_key="project_123",
        recipient_organization_id=None,
        project_id="project_123",
    )
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.main.get_client")
def test_memory_share_without_preview_is_denied(mock_get_client: MagicMock) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["memory-share", "raw-1", "--target-scope", "organization"],
    )

    assert result.exit_code == 1
    assert "only supports --preview" in result.stdout
    mock_get_client.assert_not_called()


@patch("sibyl_cli.main.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.main.get_client")
def test_recall_command_can_request_agent_diary_context(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.context_pack = AsyncMock(return_value={"goal": "ship faster", "markdown": ""})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["recall", "ship faster", "--agent", "nova"])

    assert result.exit_code == 0
    mock_client.context_pack.assert_awaited_once_with(
        goal="ship faster",
        intent="build",
        layer="recall",
        domain=None,
        project="project_123",
        agent_id="nova",
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
        persist_review=False,
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
        persist_review=False,
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
        persist_review=False,
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
def test_reflect_command_can_persist_review_queue(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.reflect = AsyncMock(
        return_value={
            "source_title": "Planning",
            "source_id": "raw-source-1",
            "persisted_count": 1,
            "total_candidates": 1,
            "candidates": [{"kind": "decision", "persisted_id": "raw-candidate-1"}],
            "markdown": "# Sibyl Reflection: Planning",
        }
    )
    mock_client.explore = AsyncMock(return_value={"entities": []})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reflect",
            "We decided reviewed candidates should wait for promotion.",
            "--persist",
            "--review",
        ],
    )

    assert result.exit_code == 0
    assert mock_client.reflect.await_args.kwargs["persist"] is True
    assert mock_client.reflect.await_args.kwargs["persist_review"] is True
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
