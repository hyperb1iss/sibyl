"""Tests for context pack CLI output."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli.client import SibylClientError
from sibyl_cli.config_store import Context
from sibyl_cli.main import app


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


def _context_pack() -> dict:
    return {
        "goal": "ship faster",
        "intent": "build",
        "query": "ship faster agent memory",
        "domain": "agent memory",
        "project": "project_123",
        "sections": [
            {
                "facet": "decisions",
                "title": "Decisions",
                "items": [
                    {
                        "id": "decision_1",
                        "type": "decision",
                        "name": "Use context packs",
                        "content": "Agents should receive precise grouped memory.",
                        "score": 0.91,
                        "facet": "decisions",
                        "reason": "decision records a choice or rationale the agent should preserve",
                        "source": None,
                        "quality": {
                            "origin": "graph",
                            "source": "docs/architecture/SIBYL_NORTHSTAR.md",
                            "url": None,
                            "created_at": None,
                            "updated_at": None,
                            "valid_at": None,
                            "project_id": "project_123",
                        },
                        "metadata": {},
                    }
                ],
            }
        ],
        "total_items": 1,
        "usage_metadata": {
            "usage_exposure": {
                "source_surface": "context_pack",
                "returned_count": 1,
                "stamped_count": 1,
                "excluded_count": 0,
                "coverage_complete": True,
            }
        },
        "usage_hint": "Capture new memory back into Sibyl.",
        "markdown": "# Sibyl Context Pack: ship faster\n\n## Decisions\n- **Use context packs**",
    }


@patch("sibyl_cli.context_quick.read_server_credentials")
@patch("sibyl_cli.context_quick.resolve_project_from_cwd", return_value="project_linked")
@patch(
    "sibyl_cli.context_quick.get_active_context",
    return_value=Context(
        name="local",
        server_url="http://localhost:3334",
        org_slug=None,
        default_project="project_default",
    ),
)
def test_context_quick_json_returns_flat_local_status(
    mock_get_active_context: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
    mock_read_server_credentials: MagicMock,
) -> None:
    mock_read_server_credentials.return_value = {
        "access_token": "token",
        "access_token_expires_at": 4_102_444_800,
    }

    runner = CliRunner()
    result = runner.invoke(app, ["context", "--quick", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["server"] == "http://localhost:3334"
    assert payload["org"] == "auto"
    assert payload["project"] == "project_linked"
    assert payload["project_source"] == "linked"
    assert payload["auth"] == "valid"
    assert payload["auth_expires_in"] > 0
    mock_get_active_context.assert_called_once_with()
    mock_resolve_project_from_cwd.assert_called_once_with()
    mock_read_server_credentials.assert_called_once_with(
        "http://localhost:3334/api",
        credential_scope="context:local:org:default",
    )


@patch("sibyl_cli.context_quick.read_server_credentials", return_value={})
@patch("sibyl_cli.context_quick.resolve_project_from_cwd", return_value=None)
@patch("sibyl_cli.context_quick.get_effective_server_url", return_value="http://localhost:3334")
@patch("sibyl_cli.context_quick.get_active_context", return_value=None)
def test_context_quick_without_context_reports_missing_auth(
    mock_get_active_context: MagicMock,
    mock_get_effective_server_url: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
    mock_read_server_credentials: MagicMock,
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["context", "--quick"])

    assert result.exit_code == 0
    assert "Project:" in result.stdout
    assert "not linked" in result.stdout
    assert "missing" in result.stdout
    mock_get_active_context.assert_called_once_with()
    mock_get_effective_server_url.assert_called_once_with()
    mock_resolve_project_from_cwd.assert_called_once_with()
    mock_read_server_credentials.assert_called_once_with(
        "http://localhost:3334/api",
        credential_scope=None,
    )


@patch("sibyl_cli.context.get_client")
@patch(
    "sibyl_cli.context.list_accessible_projects",
    new_callable=AsyncMock,
    return_value=[{"id": "project_123", "name": "Sibyl"}],
)
@patch("sibyl_cli.context.resolve_project_from_cwd", return_value="project_123")
@patch(
    "sibyl_cli.context.get_active_context",
    return_value=Context(
        name="local",
        server_url="http://localhost:3334",
        org_slug=None,
        default_project=None,
    ),
)
def test_context_uses_summary_only_project_fetch(
    mock_get_active_context: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
    mock_list_accessible_projects: AsyncMock,
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(
        return_value={
            "id": "project_123",
            "name": "Sibyl",
            "metadata": {
                "total_tasks": 1,
                "status_counts": {"doing": 1},
                "progress_pct": 0,
                "actionable_tasks": [
                    {
                        "id": "task_123",
                        "name": "Ship full-fidelity context",
                        "status": "doing",
                    }
                ],
            },
            "related": None,
        }
    )
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(app, ["context"])

    assert result.exit_code == 0
    assert "Ship full-fidelity context" in result.stdout
    mock_client.get_entity.assert_awaited_once_with("project_123", related_limit=0)
    mock_list_accessible_projects.assert_awaited_once_with(mock_client)
    mock_get_active_context.assert_called_once_with()
    assert mock_resolve_project_from_cwd.call_count == 2


@patch("sibyl_cli.context.get_client")
@patch("sibyl_cli.context.list_accessible_projects", new_callable=AsyncMock, return_value=[])
@patch("sibyl_cli.context.resolve_project_from_cwd", return_value="project_missing")
@patch(
    "sibyl_cli.context.get_active_context",
    return_value=Context(
        name="local",
        server_url="http://localhost:3334",
        org_slug=None,
        default_project=None,
    ),
)
def test_context_warns_when_linked_project_has_graph_entity_but_no_registry_record(
    mock_get_active_context: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
    mock_list_accessible_projects: AsyncMock,
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(return_value={"id": "project_missing", "name": "Sibyl"})
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(app, ["context"])

    assert result.exit_code == 0
    assert "Linked project project_missing is missing server-side" in result.stdout
    assert "sibyl project relink" in result.stdout
    assert "Sibyl" not in result.stdout
    mock_client.get_entity.assert_awaited_once_with("project_missing", related_limit=0)
    mock_list_accessible_projects.assert_awaited_once_with(mock_client)
    mock_get_active_context.assert_called_once_with()
    assert mock_resolve_project_from_cwd.call_count == 2


@patch("sibyl_cli.context.get_client")
@patch("sibyl_cli.context.resolve_project_from_cwd", return_value="project_missing")
@patch(
    "sibyl_cli.context.get_active_context",
    return_value=Context(
        name="local",
        server_url="http://localhost:3334",
        org_slug=None,
        default_project=None,
    ),
)
def test_context_warns_when_linked_project_is_missing(
    mock_get_active_context: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(side_effect=SibylClientError("missing", status_code=404))
    mock_get_client.return_value = mock_client

    runner = CliRunner()
    result = runner.invoke(app, ["context"])

    assert result.exit_code == 0
    assert "Linked project project_missing is missing server-side" in result.stdout
    assert "sibyl project relink" in result.stdout
    mock_get_active_context.assert_called_once_with()
    assert mock_resolve_project_from_cwd.call_count == 2


@patch("sibyl_cli.context.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.context.get_client")
def test_context_pack_json_uses_detected_project(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.context_pack = AsyncMock(return_value=_context_pack())
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["context", "pack", "ship faster", "--domain", "agent memory", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["sections"][0]["items"][0]["id"] == "decision_1"
    assert payload["sections"][0]["items"][0]["quality"]["project_id"] == "project_123"
    assert payload["usage_metadata"]["usage_exposure"]["coverage_complete"] is True
    mock_client.context_pack.assert_called_once_with(
        goal="ship faster",
        intent="build",
        layer="recall",
        domain="agent memory",
        project="project_123",
        agent_id=None,
        limit=24,
        include_related=True,
        related_limit=3,
        audit=False,
        markdown_token_budget=None,
    )
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.context.resolve_project_from_cwd")
@patch("sibyl_cli.context.get_client")
def test_context_pack_all_projects_omits_project_scope(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.context_pack = AsyncMock(return_value=_context_pack() | {"project": None})
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["context", "pack", "ship faster", "--all"])

    assert result.exit_code == 0
    assert "Use context packs" in result.stdout
    mock_client.context_pack.assert_called_once_with(
        goal="ship faster",
        intent="build",
        layer="recall",
        domain=None,
        project=None,
        agent_id=None,
        limit=24,
        include_related=True,
        related_limit=3,
        audit=False,
        markdown_token_budget=None,
    )
    mock_resolve_project_from_cwd.assert_not_called()


@patch("sibyl_cli.context.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.context.get_client")
def test_context_pack_markdown_outputs_server_rendering(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.context_pack = AsyncMock(return_value=_context_pack())
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["context", "pack", "ship faster", "--markdown"])

    assert result.exit_code == 0
    assert "# Sibyl Context Pack: ship faster" in result.stdout
    mock_resolve_project_from_cwd.assert_called_once_with()


@patch("sibyl_cli.context.resolve_project_from_cwd", return_value="project_123")
@patch("sibyl_cli.context.get_client")
def test_context_pack_can_request_agent_diary(
    mock_get_client: MagicMock,
    mock_resolve_project_from_cwd: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.context_pack = AsyncMock(return_value=_context_pack())
    mock_get_client.return_value = _FakeClientContext(mock_client)

    runner = CliRunner()
    result = runner.invoke(app, ["context", "pack", "ship faster", "--agent", "nova"])

    assert result.exit_code == 0
    mock_client.context_pack.assert_called_once_with(
        goal="ship faster",
        intent="build",
        layer="recall",
        domain=None,
        project="project_123",
        agent_id="nova",
        limit=24,
        include_related=True,
        related_limit=3,
        audit=False,
        markdown_token_budget=None,
    )
    mock_resolve_project_from_cwd.assert_called_once_with()
