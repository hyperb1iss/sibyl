"""Tests for database CLI graph restore compatibility."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from sibyl.cli import db as db_cli

runner = CliRunner()


def test_first_count_handles_dict_and_tuple_rows() -> None:
    assert db_cli._first_count([{"count": 3}]) == 3
    assert db_cli._first_count([{"deleted": 2}]) == 2
    assert db_cli._first_count([(4,)]) == 4
    assert db_cli._first_count([]) == 0


def test_clear_requires_org_id() -> None:
    result = runner.invoke(db_cli.app, ["clear", "--yes"])

    assert result.exit_code == 1
    assert "--org-id is required for graph operations" in result.output


def test_clear_uses_graph_tables() -> None:
    client = SimpleNamespace(execute_query=AsyncMock(return_value=[]))

    with (
        patch(
            "sibyl_core.services.graph.get_surreal_graph_client",
            AsyncMock(return_value=client),
        ),
        patch("sibyl_core.services.graph.prepare_graph_schema", AsyncMock()),
    ):
        result = runner.invoke(db_cli.app, ["clear", "--yes", "--org-id", "org-123"])

    assert result.exit_code == 0
    assert client.execute_query.await_count >= 1
    assert all(
        call.kwargs["group_id"] == "org-123" for call in client.execute_query.await_args_list
    )


def test_stats_requires_org_id() -> None:
    result = runner.invoke(db_cli.app, ["stats"])

    assert result.exit_code == 1
    assert "--org-id is required for graph operations" in result.output


def test_inventory_requires_org_id() -> None:
    result = runner.invoke(db_cli.app, ["inventory"])

    assert result.exit_code == 1
    assert "--org-id is required for inventory" in result.output


def test_plan_probes_requires_org_id() -> None:
    result = runner.invoke(db_cli.app, ["plan-probes"])

    assert result.exit_code == 1
    assert "--org-id is required for plan probes" in result.output


def test_inventory_collects_schema_tables_orphans_and_vectors() -> None:
    class FakeInventoryClient:
        def __init__(self, *, plane: str) -> None:
            self.plane = plane
            self.close = AsyncMock()
            self.execute_query = AsyncMock(side_effect=self._execute_query)

        async def _execute_query(self, query: str, **params: object) -> object:
            assert params.get("org_id", "org-123") == "org-123"
            if query == "INFO FOR DB;":
                return [{"tables": {f"{self.plane}_table": "DEFINE TABLE"}}]
            if query.startswith("INFO FOR TABLE"):
                table = query.removeprefix("INFO FOR TABLE ").removesuffix(";")
                return [{"indexes": {f"idx_{table}_uuid": f"DEFINE INDEX idx_{table}_uuid"}}]
            if "FROM schema_version" in query:
                return [{"name": self.plane, "version": 1, "embedding_dimension": None}]
            if "count()" in query:
                return [{"count": 2}]
            return []

    auth_client = FakeInventoryClient(plane="auth")
    content_client = FakeInventoryClient(plane="content")
    graph_client = FakeInventoryClient(plane="graph")

    with (
        patch("sibyl.persistence.surreal.auth.build_surreal_auth_client", return_value=auth_client),
        patch(
            "sibyl.persistence.surreal.content.build_surreal_content_client",
            return_value=content_client,
        ),
        patch(
            "sibyl_core.services.graph.get_surreal_graph_client",
            AsyncMock(return_value=graph_client),
        ),
    ):
        result = runner.invoke(db_cli.app, ["inventory", "--org-id", "org-123", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["org_id"] == "org-123"
    assert payload["auth"]["schema_versions"][0]["name"] == "auth"
    assert payload["content"]["tables"][0]["count"] == 2
    assert payload["graph"]["tables"][0]["indexes"][0]["name"].startswith("idx_")
    assert payload["orphans"]["content"][0]["name"] == "crawled_documents_missing_source"
    assert {vector["plane"] for vector in payload["vectors"]} == {"content", "graph"}
    auth_client.close.assert_awaited_once()
    content_client.close.assert_awaited_once()
    graph_client.close.assert_awaited_once()


def test_plan_probes_runs_graph_and_content_explain_queries() -> None:
    class FakePlanClient:
        def __init__(self) -> None:
            self.close = AsyncMock()
            self.execute_query = AsyncMock(side_effect=self._execute_query)

        async def _execute_query(self, query: str, **_params: object) -> object:
            if "fact_embedding" in query:
                index = "idx_relates_fact_embedding"
            elif "document_chunks" in query and "content @0@" in query:
                index = "idx_document_chunks_content_ft"
            elif "document_chunks" in query:
                index = "idx_document_chunks_embedding"
            else:
                index = "idx_entity_embedding"
            return [{"operation": "Iterate Index", "detail": {"plan": {"index": index}}}]

    content_client = FakePlanClient()
    graph_client = FakePlanClient()

    with (
        patch(
            "sibyl.persistence.surreal.content.build_surreal_content_client",
            return_value=content_client,
        ),
        patch(
            "sibyl_core.services.graph.get_surreal_graph_client",
            AsyncMock(return_value=graph_client),
        ),
    ):
        result = runner.invoke(
            db_cli.app,
            [
                "plan-probes",
                "--org-id",
                "org-123",
                "--source-id",
                "source-123",
                "--project-id",
                "project-123",
                "--graph-embedding-dim",
                "4",
                "--content-embedding-dim",
                "4",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["org_id"] == "org-123"
    assert len(payload["probes"]) == 5
    assert {probe["plane"] for probe in payload["probes"]} == {"graph", "content"}
    assert all(probe["analysis"]["uses_expected_index"] for probe in payload["probes"])
    assert content_client.execute_query.await_count == 2
    assert graph_client.execute_query.await_count == 3
    content_client.close.assert_awaited_once()
    graph_client.close.assert_awaited_once()


def test_restore_accepts_graph_export_payload(tmp_path: Path) -> None:
    graph_file = tmp_path / "graph-export.json"
    graph_file.write_text(
        json.dumps(
            {
                "metadata": {
                    "exported_at": "2026-04-19T10:00:00+00:00",
                    "entity_count": 2,
                    "relationship_count": 1,
                },
                "entities": [{"id": "entity-1"}, {"id": "entity-2"}],
                "relationships": [{"id": "rel-1"}],
            }
        ),
        encoding="utf-8",
    )

    restore_backup = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            entities_restored=2,
            relationships_restored=1,
            entities_skipped=0,
            relationships_skipped=0,
            duration_seconds=0.1,
            errors=[],
        )
    )

    with (
        patch("sibyl.cli.db._prepare_graph_runtime_async", AsyncMock()),
        patch("sibyl_core.tools.admin.restore_backup", restore_backup),
    ):
        result = runner.invoke(
            db_cli.app,
            ["restore", str(graph_file), "--org-id", "org-123", "--yes"],
        )

    assert result.exit_code == 0
    backup_data = restore_backup.await_args.args[0]
    assert backup_data.version == "2.0"
    assert backup_data.created_at == "2026-04-19T10:00:00+00:00"
    assert backup_data.organization_id == "org-123"
    assert backup_data.entity_count == 2
    assert backup_data.relationship_count == 1
    assert len(backup_data.entities) == 2
    assert len(backup_data.relationships) == 1
    assert restore_backup.await_args.kwargs == {
        "organization_id": "org-123",
        "skip_existing": True,
    }


def test_restore_prefers_top_level_backup_metadata(tmp_path: Path) -> None:
    graph_file = tmp_path / "graph-backup.json"
    graph_file.write_text(
        json.dumps(
            {
                "version": "3.0",
                "created_at": "2026-04-19T11:00:00+00:00",
                "organization_id": "org-backup",
                "entity_count": 7,
                "relationship_count": 5,
                "metadata": {
                    "exported_at": "stale",
                    "entity_count": 1,
                    "relationship_count": 1,
                },
                "entities": [{"id": "entity-1"}],
                "relationships": [{"id": "rel-1"}],
            }
        ),
        encoding="utf-8",
    )

    restore_backup = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            entities_restored=1,
            relationships_restored=1,
            entities_skipped=0,
            relationships_skipped=0,
            duration_seconds=0.1,
            errors=[],
        )
    )

    with (
        patch("sibyl.cli.db._prepare_graph_runtime_async", AsyncMock()),
        patch("sibyl_core.tools.admin.restore_backup", restore_backup),
    ):
        result = runner.invoke(
            db_cli.app,
            ["restore", str(graph_file), "--org-id", "org-override", "--yes"],
        )

    assert result.exit_code == 0
    backup_data = restore_backup.await_args.args[0]
    assert backup_data.version == "3.0"
    assert backup_data.created_at == "2026-04-19T11:00:00+00:00"
    assert backup_data.organization_id == "org-backup"
    assert backup_data.entity_count == 7
    assert backup_data.relationship_count == 5


def test_restore_prepares_graph_runtime_before_restore(tmp_path: Path) -> None:
    graph_file = tmp_path / "graph-export.json"
    graph_file.write_text(
        json.dumps(
            {
                "entities": [{"id": "entity-1"}],
                "relationships": [],
            }
        ),
        encoding="utf-8",
    )

    restore_backup = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            entities_restored=1,
            relationships_restored=0,
            entities_skipped=0,
            relationships_skipped=0,
            duration_seconds=0.1,
            errors=[],
        )
    )

    prepare = AsyncMock()

    with (
        patch(
            "sibyl.cli.db._prepare_graph_runtime",
            side_effect=AssertionError("sync helper should not be used"),
        ),
        patch("sibyl.cli.db._prepare_graph_runtime_async", prepare),
        patch("sibyl_core.tools.admin.restore_backup", restore_backup),
    ):
        result = runner.invoke(
            db_cli.app,
            ["restore", str(graph_file), "--org-id", "org-123", "--yes"],
        )

    assert result.exit_code == 0
    prepare.assert_awaited_once_with("org-123", clean=False)


def test_prepare_graph_runtime_bootstraps_native_schema_and_clears_rows() -> None:
    client = SimpleNamespace(execute_query=AsyncMock(return_value=[]))
    bootstrap_schema = AsyncMock()

    with (
        patch(
            "sibyl_core.services.graph.get_surreal_graph_client",
            AsyncMock(return_value=client),
        ),
        patch("sibyl_core.backends.surreal.schema.bootstrap_schema", bootstrap_schema),
    ):
        db_cli._prepare_graph_runtime("org-123", clean=True)

    bootstrap_schema.assert_awaited_once_with(client, reset=True)
    assert client.execute_query.await_count >= 1
    assert all(
        call.kwargs["group_id"] == "org-123" for call in client.execute_query.await_args_list
    )


def test_backup_create_uses_database_dump_request_field() -> None:
    with patch("sibyl.cli.db._api_request", return_value={"job_id": "job-123"}) as api_request:
        result = runner.invoke(
            db_cli.app,
            ["backup-create", "--no-database-dump"],
        )

    assert result.exit_code == 0
    assert api_request.call_args.args == ("POST", "/backups")
    assert api_request.call_args.kwargs["json_data"] == {
        "include_database_dump": False,
        "include_graph": True,
    }
