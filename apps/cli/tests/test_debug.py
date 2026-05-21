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


@patch("sibyl_cli.debug.pending_write_status")
@patch("sibyl_cli.debug.get_client")
def test_debug_status_displays_surreal_observability(
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
            "coordination_backend": "local",
            "coordination_status": "ok",
            "coordination_durable": True,
            "uptime_seconds": 60,
            "entity_count": 10,
            "queue_depth": 0,
            "recent_errors": [],
            "surreal_observability": {
                "configured": True,
                "health_http_status": 200,
                "metrics_http_status": 404,
                "metrics_available": False,
                "metric_count": 0,
            },
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)
    mock_pending_write_status.return_value = {"count": 0, "metrics": {}}

    result = CliRunner().invoke(debug.app, ["status"])

    assert result.exit_code == 0
    assert "Surreal:" in result.stdout
    assert "health 200" in result.stdout
    assert "metrics 404" in result.stdout


@patch("sibyl_cli.debug.get_client")
def test_debug_query_explain_prefixes_query_and_formats_plan(
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.post = AsyncMock(
        return_value={
            "rows": [
                {
                    "operator": "TableScan",
                    "context": "Db",
                    "attributes": {"table": "entity"},
                    "metrics": {
                        "elapsed_ns": 1250,
                        "output_batches": 1,
                        "output_rows": 2,
                    },
                    "total_rows": 2,
                }
            ],
            "row_count": 1,
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        debug.app,
        ["query", "--explain", "SELECT name FROM entity LIMIT 2;"],
    )

    assert result.exit_code == 0
    mock_client.post.assert_awaited_once_with(
        "/admin/debug/query",
        json={"cypher": "EXPLAIN ANALYZE FORMAT JSON SELECT name FROM entity LIMIT 2;"},
    )
    assert "TableScan" in result.stdout
    assert "entity" in result.stdout
    assert "1.25us" in result.stdout
