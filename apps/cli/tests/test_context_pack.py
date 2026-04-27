"""Tests for context pack CLI output."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

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
        "usage_hint": "Capture new memory back into Sibyl.",
        "markdown": "# Sibyl Context Pack: ship faster\n\n## Decisions\n- **Use context packs**",
    }


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
    mock_client.context_pack.assert_called_once_with(
        goal="ship faster",
        intent="build",
        layer="recall",
        domain="agent memory",
        project="project_123",
        limit=24,
        include_related=True,
        related_limit=3,
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
        limit=24,
        include_related=True,
        related_limit=3,
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
