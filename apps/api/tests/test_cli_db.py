"""Tests for database CLI graph restore compatibility."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import typer
from typer.testing import CliRunner

from sibyl.cli import db as db_cli

runner = CliRunner()


def test_legacy_fix_embeddings_command_is_not_registered() -> None:
    command_names = {command.name for command in db_cli.app.registered_commands}

    assert "fix-embeddings" not in command_names


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


def test_backup_create_wait_fails_when_the_server_forgot_the_job() -> None:
    """--wait never saw the archive, so it must not exit green."""
    with patch(
        "sibyl.cli.db._api_request",
        side_effect=[{"job_id": "job-123"}, None],
    ) as api_request:
        result = runner.invoke(db_cli.app, ["backup-create", "--wait"])

    assert result.exit_code == 1
    assert api_request.call_args.args == ("GET", "/backups/jobs/job-123")
    assert api_request.call_args.kwargs["missing_message"] == "Job not found: job-123"
    assert "Job not found" in result.output


def test_backup_create_wait_succeeds_on_a_confirmed_archive() -> None:
    archive = {"success": True, "archive_path": "backups/a.tar.gz"}
    complete = {"status": "complete", "result": archive}
    with patch("sibyl.cli.db._api_request", side_effect=[{"job_id": "job-123"}, complete]):
        result = runner.invoke(db_cli.app, ["backup-create", "--wait"])

    assert result.exit_code == 0
    assert "Backup complete!" in result.output


def test_backup_create_wait_stops_on_a_cancelled_job() -> None:
    """cancelled is terminal, so polling it forever would hang the command."""
    cancelled = {"status": "cancelled", "result": None}
    with patch(
        "sibyl.cli.db._api_request",
        side_effect=[{"job_id": "job-123"}, cancelled],
    ) as api_request:
        result = runner.invoke(db_cli.app, ["backup-create", "--wait"])

    assert result.exit_code == 1
    assert api_request.call_count == 2
    assert "cancelled" in result.output


def _client_raising(status_code: int, body: object, text: str = "") -> MagicMock:
    response = MagicMock(status_code=status_code, text=text)
    if isinstance(body, Exception):
        response.json.side_effect = body
    else:
        response.json.return_value = body
    client = MagicMock()
    client.get.return_value.raise_for_status.side_effect = httpx.HTTPStatusError(
        "error", request=None, response=response
    )
    return client


def _get_with(client: MagicMock, **kwargs: object) -> object:
    with patch("httpx.Client") as client_factory:
        client_factory.return_value.__enter__.return_value = client
        return db_cli._api_request("GET", "/backups/jobs/job-123", **kwargs)


def test_api_request_reports_the_apis_own_missing_job() -> None:
    client = _client_raising(404, {"error": "not_found", "message": "Job not found: job-123"})

    assert _get_with(client, missing_message="Job not found: job-123") is None


def test_api_request_does_not_read_an_unrelated_404_as_a_missing_job() -> None:
    """A proxy or a route the server does not serve must not look like success."""
    unrelated = [
        _client_raising(404, {"error": "not_found", "message": "Not Found"}),
        _client_raising(404, {"detail": "Not Found"}),
        _client_raising(404, ValueError("not json"), text="<html>404</html>"),
        _client_raising(404, {"error": "not_found", "message": "Job not found: other-job"}),
    ]

    for client in unrelated:
        with pytest.raises(typer.Exit) as exit_info:
            _get_with(client, missing_message="Job not found: job-123")

        assert exit_info.value.exit_code == 1


class _StubSchemaClient:
    """Minimal Surreal stand-in for the schema-init introspection queries."""

    def __init__(
        self,
        *,
        version: int,
        tables: dict[str, str] | None = None,
        indexes: dict[str, dict[str, str]] | None = None,
        version_read_error: str = "",
    ) -> None:
        self.version = version
        self.tables = tables or {}
        self.indexes = indexes or {}
        self.version_read_error = version_read_error
        self.statements: list[str] = []

    async def execute_query(self, statement: str, **_params: object) -> object:
        self.statements.append(statement)
        stripped = statement.strip()
        if stripped.startswith("SELECT version FROM schema_version"):
            if self.version_read_error:
                raise RuntimeError(self.version_read_error)
            return [{"version": self.version}]
        if stripped == "INFO FOR DB;":
            return [{"tables": self.tables}]
        if stripped.startswith("INFO FOR TABLE "):
            table = stripped.removeprefix("INFO FOR TABLE ").rstrip(";").strip()
            return [{"indexes": self.indexes.get(table, {})}]
        if stripped.startswith("DEFINE INDEX"):
            raise RuntimeError("Database index already contains 'dirty'")
        return None

    async def close(self) -> None:
        return None


def _run_db_init(auth_client: object, content_client: object, *, args: list[str] | None = None):
    from sibyl_core.backends.surreal.schema_invariants import SchemaInvariantPlan

    empty_plan = SchemaInvariantPlan()
    with (
        patch("sibyl.persistence.surreal.auth.build_surreal_auth_client", lambda: auth_client),
        patch(
            "sibyl.persistence.surreal.content.build_surreal_content_client",
            lambda: content_client,
        ),
        patch("sibyl_core.backends.surreal.bootstrap_auth_schema", AsyncMock()),
        patch("sibyl_core.backends.surreal.bootstrap_content_schema", AsyncMock()),
        patch(
            "sibyl_core.backends.surreal.auth_schema.auth_schema_invariant_plan",
            lambda: empty_plan,
        ),
        patch(
            "sibyl_core.backends.surreal.content_schema.content_schema_invariant_plan",
            lambda **_kwargs: empty_plan,
        ),
        patch("sibyl.config.settings.store", "surreal"),
    ):
        return runner.invoke(db_cli.app, ["init", *(args or [])])


def test_db_init_reports_a_version_read_failure_instead_of_assuming_zero() -> None:
    """A store that cannot be read must not look like a fresh one that migrated cleanly."""
    from sibyl_core.backends.surreal.auth_schema import AUTH_SCHEMA_CURRENT_VERSION
    from sibyl_core.backends.surreal.content_schema import CONTENT_SCHEMA_CURRENT_VERSION

    result = _run_db_init(
        _StubSchemaClient(version=AUTH_SCHEMA_CURRENT_VERSION, version_read_error="socket closed"),
        _StubSchemaClient(version=CONTENT_SCHEMA_CURRENT_VERSION),
    )

    assert result.exit_code == 1
    assert "schema state could not be read" in result.output
    assert "socket closed" in result.output


def test_db_init_fails_when_migrations_stop_short_of_the_target() -> None:
    from sibyl_core.backends.surreal.auth_schema import AUTH_SCHEMA_CURRENT_VERSION
    from sibyl_core.backends.surreal.content_schema import CONTENT_SCHEMA_CURRENT_VERSION

    result = _run_db_init(
        _StubSchemaClient(version=AUTH_SCHEMA_CURRENT_VERSION),
        _StubSchemaClient(version=CONTENT_SCHEMA_CURRENT_VERSION - 1),
    )

    assert result.exit_code == 1
    assert f"expected v{CONTENT_SCHEMA_CURRENT_VERSION}" in result.output


def test_db_init_succeeds_when_both_planes_reach_their_target() -> None:
    from sibyl_core.backends.surreal.auth_schema import AUTH_SCHEMA_CURRENT_VERSION
    from sibyl_core.backends.surreal.content_schema import CONTENT_SCHEMA_CURRENT_VERSION

    result = _run_db_init(
        _StubSchemaClient(version=AUTH_SCHEMA_CURRENT_VERSION),
        _StubSchemaClient(version=CONTENT_SCHEMA_CURRENT_VERSION),
    )

    assert result.exit_code == 0
    assert "Schema up to date" in result.output


def test_db_init_fails_when_a_required_unique_index_is_missing() -> None:
    """Reaching the target version is not enough if a skipped index left dedupe unenforced."""
    from sibyl_core.backends.surreal.auth_schema import AUTH_SCHEMA_CURRENT_VERSION
    from sibyl_core.backends.surreal.content_schema import CONTENT_SCHEMA_CURRENT_VERSION
    from sibyl_core.backends.surreal.schema_invariants import (
        SchemaInvariantPlan,
        UniqueIndexRequirement,
    )

    plan = SchemaInvariantPlan(
        schemafull_tables=("memory_usage_events",),
        unique_indexes=(
            UniqueIndexRequirement(
                name="idx_memory_usage_events_uuid",
                table="memory_usage_events",
                fields=("uuid",),
                statement=(
                    "DEFINE INDEX IF NOT EXISTS idx_memory_usage_events_uuid "
                    "ON memory_usage_events FIELDS uuid UNIQUE;"
                ),
            ),
        ),
    )
    content_client = _StubSchemaClient(
        version=CONTENT_SCHEMA_CURRENT_VERSION,
        tables={"memory_usage_events": "DEFINE TABLE memory_usage_events TYPE ANY SCHEMAFULL"},
        indexes={"memory_usage_events": {}},
    )

    with (
        patch(
            "sibyl.persistence.surreal.auth.build_surreal_auth_client",
            lambda: _StubSchemaClient(version=AUTH_SCHEMA_CURRENT_VERSION),
        ),
        patch(
            "sibyl.persistence.surreal.content.build_surreal_content_client",
            lambda: content_client,
        ),
        patch("sibyl_core.backends.surreal.bootstrap_auth_schema", AsyncMock()),
        patch("sibyl_core.backends.surreal.bootstrap_content_schema", AsyncMock()),
        patch(
            "sibyl_core.backends.surreal.auth_schema.auth_schema_invariant_plan",
            SchemaInvariantPlan,
        ),
        patch(
            "sibyl_core.backends.surreal.content_schema.content_schema_invariant_plan",
            lambda **_kwargs: plan,
        ),
        patch("sibyl.config.settings.store", "surreal"),
    ):
        result = runner.invoke(db_cli.app, ["init"])

    assert result.exit_code == 1
    assert "unmet" in result.output
    assert "idx_memory_usage_events_uuid" in result.output
    assert "sibyld db duplicates" in result.output


class _EmbeddedClient:
    """Adapter that gives the CLI its client shape over an embedded SurrealDB."""

    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def execute_query(self, statement: str, **params: object) -> object:
        return await self._connection.query(statement, params or None)

    async def close(self) -> None:
        return None


def _usage_event(uuid: str, item_id: str) -> str:
    return f"""
    CREATE memory_usage_events CONTENT {{
        uuid: '{uuid}',
        organization_id: 'org-a',
        session_key: 's',
        message_key: 'm',
        source_surface: 'recall',
        item_kind: 'raw_capture',
        item_id: '{item_id}',
        signal_type: 'exposure',
        metadata: {{}},
        event_at: d'2026-07-11T00:00:00Z',
        created_at: d'2026-07-11T00:00:00Z'
    }};
    """


async def _seeded_usage_store():
    from surrealdb import AsyncSurreal

    connection = AsyncSurreal("memory://")
    await connection.use("cli_duplicates", "content")
    for item_id in ("item-0", "item-1", "item-2"):
        await connection.query(_usage_event("dup", item_id))
    await connection.query(_usage_event("solo", "item-3"))
    return connection


def _run_duplicates(connection: object, args: list[str]):
    from sibyl_core.backends.surreal.content_schema import content_schema_invariant_plan
    from sibyl_core.backends.surreal.schema_invariants import SchemaInvariantPlan

    plan = content_schema_invariant_plan()
    usage_only = SchemaInvariantPlan(
        unique_indexes=tuple(
            item for item in plan.unique_indexes if item.table == "memory_usage_events"
        ),
    )
    client = _EmbeddedClient(connection)
    with (
        patch(
            "sibyl.persistence.surreal.auth.build_surreal_auth_client",
            lambda: _StubSchemaClient(version=0),
        ),
        patch("sibyl.persistence.surreal.content.build_surreal_content_client", lambda: client),
        patch(
            "sibyl_core.backends.surreal.auth_schema.auth_schema_invariant_plan",
            SchemaInvariantPlan,
        ),
        patch(
            "sibyl_core.backends.surreal.content_schema.content_schema_invariant_plan",
            lambda **_kwargs: usage_only,
        ),
        patch("sibyl.config.settings.store", "surreal"),
    ):
        return runner.invoke(db_cli.app, ["duplicates", *args])


def test_duplicates_inventories_rows_blocking_a_missing_unique_index() -> None:
    import asyncio

    connection = asyncio.run(_seeded_usage_store())
    try:
        result = _run_duplicates(connection, ["--json"])
    finally:
        asyncio.run(connection.close())

    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["excess_rows"] == 2
    assert payload["collapsed"] == 0
    uuid_groups = [item for item in payload["groups"] if item["index"].endswith("_uuid")]
    assert uuid_groups[0]["copies"] == 3
    assert uuid_groups[0]["key"] == ["dup"]


def test_duplicates_collapse_keeps_one_row_per_group() -> None:
    import asyncio

    connection = asyncio.run(_seeded_usage_store())
    try:
        result = _run_duplicates(connection, ["--collapse", "--yes", "--json"])
        remaining = asyncio.run(
            connection.query("SELECT uuid FROM memory_usage_events ORDER BY uuid;")
        )
    finally:
        asyncio.run(connection.close())

    assert result.exit_code == 0
    assert json.loads(result.output)["collapsed"] == 2
    assert [row["uuid"] for row in remaining] == ["dup", "solo"]


async def _seeded_users_store():
    """Users whose optional github_id is absent, plus one genuinely duplicated pair."""
    from surrealdb import AsyncSurreal

    connection = AsyncSurreal("memory://")
    await connection.use("cli_users", "auth")
    for uuid, email, github_id in (
        ("u1", "a@example.com", None),
        ("u2", "b@example.com", None),
        ("u3", "c@example.com", 7),
        ("u4", "d@example.com", 7),
    ):
        github = "" if github_id is None else f", github_id: {github_id}"
        await connection.query(
            f"CREATE users CONTENT {{ uuid: '{uuid}', email: '{email}'{github} }};"
        )
    return connection


def _run_auth_duplicates(connection: object, args: list[str]):
    from sibyl_core.backends.surreal.auth_schema import auth_schema_invariant_plan
    from sibyl_core.backends.surreal.schema_invariants import SchemaInvariantPlan

    plan = auth_schema_invariant_plan()
    users_only = SchemaInvariantPlan(
        unique_indexes=tuple(item for item in plan.unique_indexes if item.table == "users"),
    )
    client = _EmbeddedClient(connection)
    with (
        patch("sibyl.persistence.surreal.auth.build_surreal_auth_client", lambda: client),
        patch(
            "sibyl_core.backends.surreal.auth_schema.auth_schema_invariant_plan",
            lambda: users_only,
        ),
        patch("sibyl.config.settings.store", "relational"),
    ):
        return runner.invoke(db_cli.app, ["duplicates", *args])


def test_duplicates_never_groups_rows_whose_unique_key_component_is_missing() -> None:
    """SurrealDB lets many rows share a UNIQUE index key that has a missing component.

    `github_id` is `option<int>`, so every user who never linked GitHub is a legitimately
    distinct row. Bucketing them together would make --collapse delete real accounts.
    """
    import asyncio

    connection = asyncio.run(_seeded_users_store())
    try:
        result = _run_auth_duplicates(connection, ["--json"])
    finally:
        asyncio.run(connection.close())

    payload = json.loads(result.output)
    github_groups = [item for item in payload["groups"] if item["index"].endswith("github_id")]

    assert result.exit_code == 0
    assert payload["excess_rows"] == 1
    assert [item["key"] for item in github_groups] == [["7"]]


def test_duplicates_collapse_spares_rows_with_a_missing_unique_key_component() -> None:
    import asyncio

    connection = asyncio.run(_seeded_users_store())
    try:
        result = _run_auth_duplicates(connection, ["--collapse", "--yes", "--json"])
        remaining = asyncio.run(connection.query("SELECT uuid FROM users ORDER BY uuid;"))
    finally:
        asyncio.run(connection.close())

    survivors = [row["uuid"] for row in remaining]

    assert result.exit_code == 0
    assert json.loads(result.output)["collapsed"] == 1
    # Both github-less users survive; the duplicated pair keeps whichever record id
    # sorts first, which is not tied to the uuid.
    assert {"u1", "u2"}.issubset(survivors)
    assert len([uuid for uuid in survivors if uuid in {"u3", "u4"}]) == 1
    assert len(survivors) == 3


class _StubDuplicateClient:
    """Serves one table with three colliding rows but deletes fewer than it is asked to."""

    def __init__(self) -> None:
        self.deleted_batches: list[int] = []

    async def execute_query(self, statement: str, **params: object) -> object:
        stripped = statement.strip()
        if stripped == "INFO FOR DB;":
            return [{"tables": {"widgets": "DEFINE TABLE widgets TYPE ANY SCHEMAFULL"}}]
        if stripped.startswith("INFO FOR TABLE "):
            return [{"indexes": {}}]
        if stripped.startswith("SELECT id AS duplicate_record_id"):
            return [
                {"duplicate_record_id": f"widgets:{suffix}", "uuid": "same"}
                for suffix in ("a", "b", "c")
            ]
        if stripped.startswith("DELETE"):
            ids = params.get("ids")
            self.deleted_batches.append(len(ids) if isinstance(ids, list) else 0)
            # Only one of the two requested rows was still present.
            return [{"id": "widgets:b", "uuid": "same"}]
        return None

    async def close(self) -> None:
        return None


def test_duplicates_collapse_counts_rows_actually_removed_not_ids_requested() -> None:
    """A bare DELETE returns nothing, so the count has to come from the query result."""
    from sibyl_core.backends.surreal.schema_invariants import (
        SchemaInvariantPlan,
        UniqueIndexRequirement,
    )

    client = _StubDuplicateClient()
    plan = SchemaInvariantPlan(
        unique_indexes=(
            UniqueIndexRequirement(
                name="idx_widgets_uuid",
                table="widgets",
                fields=("uuid",),
                statement="DEFINE INDEX idx_widgets_uuid ON widgets FIELDS uuid UNIQUE;",
            ),
        ),
    )
    with (
        patch("sibyl.persistence.surreal.auth.build_surreal_auth_client", lambda: client),
        patch(
            "sibyl_core.backends.surreal.auth_schema.auth_schema_invariant_plan",
            lambda: plan,
        ),
        patch("sibyl.config.settings.store", "relational"),
    ):
        result = runner.invoke(db_cli.app, ["duplicates", "--collapse", "--yes", "--json"])

    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert client.deleted_batches == [2]
    assert payload["excess_rows"] == 2
    assert payload["collapsed"] == 1
