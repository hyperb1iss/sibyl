from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli import debug


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


@patch("sibyl_cli.debug.pending_write_status")
@patch("sibyl_cli.debug.get_client")
def test_debug_status_includes_pending_write_metrics(
    mock_get_client: MagicMock,
    mock_pending_write_status: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.get = AsyncMock(
        return_value={
            "api_healthy": True,
            "worker_healthy": True,
            "graph_healthy": True,
            "queue_healthy": True,
            "coordination_backend": "surreal",
            "coordination_status": "ok",
            "coordination_durable": True,
            "uptime_seconds": 60,
            "entity_count": 10,
            "queue_depth": 0,
            "recent_errors": [],
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)
    mock_pending_write_status.return_value = {
        "count": 2,
        "metrics": {"attempted": 3, "replayed": 1, "discarded": 1, "expired": 0},
    }

    result = CliRunner().invoke(debug.app, ["status", "--json"])

    assert result.exit_code == 0
    assert '"pending_writes"' in result.stdout
    assert '"attempted": 3' in result.stdout
