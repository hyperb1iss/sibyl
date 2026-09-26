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
        "metrics": {"attempted": 3, "completed": 1, "replayed": 1, "dropped": 0, "discarded": 1},
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


@patch("sibyl_cli.debug.pending_write_status")
@patch("sibyl_cli.debug.get_client")
def test_debug_status_shows_embedding_sweep_progress(
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
            "embedding_sweep": {
                "graph": {
                    "state": "sweeping",
                    "active_metadata": {
                        "provider": "bedrock",
                        "model": "cohere.embed-v4:0",
                        "dimensions": 1024,
                    },
                    "legacy_decision": "reembed",
                    "legacy_basis": "prior_stamps_differ",
                    "last_run": {"status": "partial", "recovered": 384, "pending": 12000},
                },
                "document_chunks": {
                    "state": "complete",
                    "complete_metadata": {"provider": "bedrock", "model": "m", "dimensions": 1536},
                    "last_run": {"status": "completed", "rejected": 3},
                },
            },
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)
    mock_pending_write_status.return_value = {"count": 0, "metrics": {}}

    result = CliRunner().invoke(debug.app, ["status"])

    assert result.exit_code == 0
    assert "Embeddings:" in result.stdout
    assert "graph sweeping bedrock/cohere.embed-v4:0/1024" in result.stdout
    assert "12,000 pending (partial)" in result.stdout
    assert "legacy reembed (prior_stamps_differ)" in result.stdout
    assert "document_chunks complete" in result.stdout
    assert "3 refused by the provider" in result.stdout


def test_embedding_lines_tell_the_operator_how_to_resolve_an_unproven_adoption() -> None:
    lines = debug._embedding_sweep_lines(
        {
            "document_chunks": {
                "state": "adopted_without_evidence",
                "complete_metadata": {"provider": "bedrock", "model": "m", "dimensions": 1536},
                "legacy_decision": "adopt",
                "legacy_basis": "no_prior_evidence",
                "legacy_warning": "adopted_without_evidence",
            }
        }
    )

    assert "document_chunks adopted_without_evidence" in lines[0]
    assert "sibyld db reembed --plane documents" in lines[1]


def test_embedding_lines_name_the_organizations_a_plane_waits_on() -> None:
    lines = debug._embedding_sweep_lines(
        {
            "graph": {
                "state": "awaiting_evidence",
                "waiting_on_count": 1,
                "waiting_on_organizations": ["broken-org"],
                "settles_in_seconds": 480,
            }
        }
    )

    assert "graph awaiting_evidence" in lines[0]
    assert "broken-org" in lines[1]
    assert "settles on the evidence published so far in 480s" in lines[1]


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


def test_embedding_lines_explain_a_provisional_adoption() -> None:
    lines = debug._embedding_sweep_lines(
        {
            "graph": {
                "state": "adopted_on_incomplete_evidence",
                "legacy_warning": "adopted_on_incomplete_evidence",
                "legacy_provisional": True,
            }
        }
    )

    assert "graph adopted_on_incomplete_evidence" in lines[0]
    assert "re-embedded on its own if late evidence shows a switch" in lines[1]
    assert not any("sibyld db reembed" in line for line in lines)
