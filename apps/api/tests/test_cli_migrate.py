"""Tests for the migration archive CLI."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from sibyl.cli import migrate as migrate_cli
from sibyl_core.migrate.archive import (
    AUTH_FILENAME,
    CONTENT_FILENAME,
    GRAPH_FILENAME,
    POSTGRES_FILENAME,
    build_manifest,
    graph_payload_from_archive,
    load_archive,
    write_archive,
)

runner = CliRunner()


def _auth_payload(*, user_rows: int = 1) -> dict[str, object]:
    return {
        "version": "1.0",
        "created_at": "2026-04-21T02:00:00+00:00",
        "tables": {
            "users": [
                {"id": f"user-{index}", "email": f"user{index}@example.com"}
                for index in range(user_rows)
            ],
            "organizations": [],
        },
        "row_counts": {"users": user_rows, "organizations": 0},
        "total_rows": user_rows,
    }


def _content_payload(*, chunk_rows: int = 1) -> dict[str, object]:
    return {
        "version": "1.0",
        "created_at": "2026-04-21T03:00:00+00:00",
        "tables": {
            "crawl_sources": [
                {
                    "id": "source-1",
                    "organization_id": "org-123",
                    "name": "Docs",
                    "url": "https://docs.example.com",
                }
            ],
            "crawled_documents": [
                {
                    "id": "document-1",
                    "source_id": "source-1",
                    "url": "https://docs.example.com/page",
                    "title": "Docs Page",
                }
            ],
            "document_chunks": [
                {
                    "id": f"chunk-{index}",
                    "document_id": "document-1",
                    "chunk_index": index,
                    "content": f"chunk {index}",
                    "embedding": [0.1, 0.2, 0.3],
                }
                for index in range(chunk_rows)
            ],
            "raw_captures": [],
            "system_settings": [],
            "backup_settings": [],
            "backups": [],
        },
        "row_counts": {
            "crawl_sources": 1,
            "crawled_documents": 1,
            "document_chunks": chunk_rows,
            "raw_captures": 0,
            "system_settings": 0,
            "backup_settings": 0,
            "backups": 0,
        },
        "total_rows": chunk_rows + 2,
    }


def _write_graph_archive(path: Path, *, org_id: str = "org-123") -> None:
    files = {
        GRAPH_FILENAME: json.dumps(
            {
                "version": "2.0",
                "created_at": "2026-04-19T20:00:00+00:00",
                "organization_id": org_id,
                "entity_count": 1,
                "relationship_count": 0,
                "entities": [{"id": "entity-1"}],
                "relationships": [],
            }
        ).encode("utf-8")
    }
    manifest = build_manifest(
        organization_id=org_id,
        source_store="legacy",
        files=files,
        file_metadata={
            GRAPH_FILENAME: {"kind": "graph", "entity_count": 1, "relationship_count": 0}
        },
    )
    write_archive(path, manifest=manifest, files=files)


def _write_full_archive(
    path: Path,
    *,
    org_id: str = "org-123",
    include_auth: bool = False,
    include_content: bool = False,
) -> None:
    files = {
        GRAPH_FILENAME: json.dumps(
            {
                "version": "2.0",
                "created_at": "2026-04-19T20:00:00+00:00",
                "organization_id": org_id,
                "entity_count": 1,
                "relationship_count": 0,
                "entities": [{"id": "entity-1"}],
                "relationships": [],
            }
        ).encode("utf-8"),
        POSTGRES_FILENAME: b"select 1;\n",
    }
    file_metadata: dict[str, dict[str, object]] = {
        GRAPH_FILENAME: {"kind": "graph", "entity_count": 1, "relationship_count": 0},
        POSTGRES_FILENAME: {"kind": "database_dump"},
    }
    if include_auth:
        auth_payload = _auth_payload()
        files[AUTH_FILENAME] = json.dumps(auth_payload).encode("utf-8")
        file_metadata[AUTH_FILENAME] = {"kind": "auth", "table_count": 2, "total_rows": 1}
    if include_content:
        content_payload = _content_payload()
        files[CONTENT_FILENAME] = json.dumps(content_payload).encode("utf-8")
        file_metadata[CONTENT_FILENAME] = {"kind": "content", "table_count": 7, "total_rows": 3}
    manifest = build_manifest(
        organization_id=org_id,
        source_store="legacy",
        files=files,
        file_metadata=file_metadata,
    )
    write_archive(path, manifest=manifest, files=files)


def _verify_result() -> SimpleNamespace:
    return SimpleNamespace(
        success=True,
        expected_entities=1,
        actual_entities=1,
        expected_relationships=0,
        actual_relationships=0,
        validated_entity_ids=["entity-1"],
        errors=[],
    )


def test_migrate_check_validates_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    result = runner.invoke(migrate_cli.app, ["check", str(archive_path)])

    assert result.exit_code == 0
    assert "Archive validation passed" in result.output


def test_migrate_merge_writes_canonical_archive(tmp_path: Path) -> None:
    first_archive = tmp_path / "first.tar.gz"
    second_archive = tmp_path / "second.tar.gz"
    output_archive = tmp_path / "merged.tar.gz"
    _write_graph_archive(first_archive, org_id="org-a")
    _write_graph_archive(second_archive, org_id="org-b")

    result = runner.invoke(
        migrate_cli.app,
        [
            "merge",
            str(first_archive),
            str(second_archive),
            "--canonical-org-id",
            "org-canonical",
            "--output",
            str(output_archive),
        ],
    )

    assert result.exit_code == 0
    assert "Merged archive written" in result.output

    loaded = load_archive(output_archive)
    graph_payload = graph_payload_from_archive(loaded)
    assert loaded.manifest.organization_id == "org-canonical"
    assert graph_payload is not None
    assert graph_payload["organization_id"] == "org-canonical"


def test_migrate_merge_dry_run_does_not_write_archive(tmp_path: Path) -> None:
    source_archive = tmp_path / "source.tar.gz"
    output_archive = tmp_path / "merged.tar.gz"
    _write_graph_archive(source_archive, org_id="org-a")

    result = runner.invoke(
        migrate_cli.app,
        [
            "merge",
            str(source_archive),
            "--canonical-org-id",
            "org-canonical",
            "--output",
            str(output_archive),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Merged archive dry run passed" in result.output
    assert not output_archive.exists()


def test_migrate_export_graph_only_writes_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    graph_payload = {
        "version": "2.0",
        "created_at": "2026-04-19T20:00:00+00:00",
        "organization_id": "org-123",
        "entity_count": 1,
        "relationship_count": 0,
        "entities": [{"id": "entity-1"}],
        "relationships": [],
    }

    with patch(
        "sibyl.cli.migrate._load_graph_export",
        return_value=(graph_payload, json.dumps(graph_payload).encode("utf-8")),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "export",
                "--org-id",
                "org-123",
                "--output",
                str(archive_path),
                "--no-include-database-dump",
                "--skip-auth",
                "--skip-content",
            ],
        )

    assert result.exit_code == 0
    assert archive_path.exists()


def test_migrate_export_writes_graph_and_postgres_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    graph_payload = {
        "version": "2.0",
        "created_at": "2026-04-19T20:00:00+00:00",
        "organization_id": "org-123",
        "entity_count": 1,
        "relationship_count": 0,
        "entities": [{"id": "entity-1"}],
        "relationships": [],
    }

    with (
        patch.object(migrate_cli.settings, "store", "legacy"),
        patch.object(migrate_cli.settings, "auth_store", "postgres"),
        patch(
            "sibyl.cli.migrate._load_graph_export",
            return_value=(graph_payload, json.dumps(graph_payload).encode("utf-8")),
        ),
        patch(
            "sibyl.cli.migrate._load_runtime_exports",
            return_value=(
                (_auth_payload(), json.dumps(_auth_payload()).encode("utf-8")),
                (_content_payload(), json.dumps(_content_payload()).encode("utf-8")),
            ),
        ),
        patch("sibyl.cli.migrate._run_pg_dump", return_value=b"select 1;\n"),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "export",
                "--org-id",
                "org-123",
                "--output",
                str(archive_path),
            ],
        )

    assert result.exit_code == 0
    loaded = load_archive(archive_path)
    assert AUTH_FILENAME in loaded.files
    assert CONTENT_FILENAME in loaded.files
    assert POSTGRES_FILENAME in loaded.files
    assert GRAPH_FILENAME in loaded.files


def test_migrate_export_auto_selects_single_org(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    graph_payload = {
        "version": "2.0",
        "created_at": "2026-04-19T20:00:00+00:00",
        "organization_id": "org-123",
        "entity_count": 1,
        "relationship_count": 0,
        "entities": [{"id": "entity-1"}],
        "relationships": [],
    }

    with (
        patch(
            "sibyl.persistence.organization_runtime.list_org_ids",
            AsyncMock(return_value=["org-123"]),
        ),
        patch(
            "sibyl.cli.migrate._load_graph_export",
            return_value=(graph_payload, json.dumps(graph_payload).encode("utf-8")),
        ) as load_graph_export,
        patch(
            "sibyl.cli.migrate._load_runtime_exports",
            return_value=(
                (_auth_payload(), json.dumps(_auth_payload()).encode("utf-8")),
                (_content_payload(), json.dumps(_content_payload()).encode("utf-8")),
            ),
        ),
        patch("sibyl.cli.migrate._run_pg_dump", return_value=b"select 1;\n"),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "export",
                "--output",
                str(archive_path),
            ],
        )

    assert result.exit_code == 0
    assert "using the only organization: org-123" in result.output
    load_graph_export.assert_called_once_with("org-123")


def test_migrate_export_requires_org_id_when_multiple_orgs_exist(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"

    with patch(
        "sibyl.persistence.organization_runtime.list_org_ids",
        AsyncMock(return_value=["org-one", "org-two"]),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "export",
                "--output",
                str(archive_path),
            ],
        )

    assert result.exit_code == 1
    assert "Multiple organizations found" in result.output
    assert "org-one" in result.output
    assert "org-two" in result.output
    assert not archive_path.exists()


def test_migrate_export_suppresses_postgres_dump_in_fully_surreal_mode(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    graph_payload = {
        "version": "2.0",
        "created_at": "2026-04-19T20:00:00+00:00",
        "organization_id": "org-123",
        "entity_count": 1,
        "relationship_count": 0,
        "entities": [{"id": "entity-1"}],
        "relationships": [],
    }

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch.object(migrate_cli.settings, "auth_store", "surreal"),
        patch(
            "sibyl.cli.migrate._load_graph_export",
            return_value=(graph_payload, json.dumps(graph_payload).encode("utf-8")),
        ),
        patch(
            "sibyl.cli.migrate._load_runtime_exports",
            return_value=(
                (_auth_payload(), json.dumps(_auth_payload()).encode("utf-8")),
                (_content_payload(), json.dumps(_content_payload()).encode("utf-8")),
            ),
        ),
        patch("sibyl.cli.migrate._run_pg_dump", side_effect=AssertionError("pg_dump disabled")),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "export",
                "--org-id",
                "org-123",
                "--output",
                str(archive_path),
            ],
        )

    assert result.exit_code == 0
    loaded = load_archive(archive_path)
    assert POSTGRES_FILENAME not in loaded.files
    assert AUTH_FILENAME in loaded.files
    assert CONTENT_FILENAME in loaded.files
    assert GRAPH_FILENAME in loaded.files


def test_migrate_export_errors_when_only_unsupported_postgres_payload_selected(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "migration.tar.gz"

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch.object(migrate_cli.settings, "auth_store", "surreal"),
        patch("sibyl.cli.migrate._run_pg_dump", side_effect=AssertionError("pg_dump disabled")),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "export",
                "--output",
                str(archive_path),
                "--skip-graph",
                "--skip-auth",
                "--skip-content",
            ],
        )

    assert result.exit_code == 1
    assert "Select at least one supported payload" in result.output
    assert not archive_path.exists()


def test_migrate_export_writes_content_archive_when_requested(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    graph_payload = {
        "version": "2.0",
        "created_at": "2026-04-19T20:00:00+00:00",
        "organization_id": "org-123",
        "entity_count": 1,
        "relationship_count": 0,
        "entities": [{"id": "entity-1"}],
        "relationships": [],
    }

    with (
        patch(
            "sibyl.cli.migrate._load_graph_export",
            return_value=(graph_payload, json.dumps(graph_payload).encode("utf-8")),
        ),
        patch(
            "sibyl.cli.migrate._load_content_export",
            return_value=(
                _content_payload(),
                json.dumps(_content_payload()).encode("utf-8"),
            ),
        ),
        patch("sibyl.cli.migrate._run_pg_dump", return_value=b"select 1;\n"),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "export",
                "--org-id",
                "org-123",
                "--output",
                str(archive_path),
                "--skip-auth",
                "--include-content",
            ],
        )

    assert result.exit_code == 0
    loaded = load_archive(archive_path)
    assert CONTENT_FILENAME in loaded.files


def test_migrate_export_loads_auth_and_content_by_default_in_one_runtime_pass(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    graph_payload = {
        "version": "2.0",
        "created_at": "2026-04-19T20:00:00+00:00",
        "organization_id": "org-123",
        "entity_count": 1,
        "relationship_count": 0,
        "entities": [{"id": "entity-1"}],
        "relationships": [],
    }
    runtime_exports = (
        (_auth_payload(), json.dumps(_auth_payload()).encode("utf-8")),
        (_content_payload(), json.dumps(_content_payload()).encode("utf-8")),
    )

    with (
        patch(
            "sibyl.cli.migrate._load_graph_export",
            return_value=(graph_payload, json.dumps(graph_payload).encode("utf-8")),
        ),
        patch(
            "sibyl.cli.migrate._load_runtime_exports", return_value=runtime_exports
        ) as load_runtime_exports,
        patch("sibyl.cli.migrate._run_pg_dump", return_value=b"select 1;\n"),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "export",
                "--org-id",
                "org-123",
                "--output",
                str(archive_path),
            ],
        )

    assert result.exit_code == 0
    load_runtime_exports.assert_called_once_with(include_auth=True, include_content=True)
    loaded = load_archive(archive_path)
    assert AUTH_FILENAME in loaded.files
    assert CONTENT_FILENAME in loaded.files


def test_migrate_import_uses_archive_org_id(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path, org_id="org-xyz")

    with patch("sibyl.cli.migrate._restore_graph_payload", return_value=True) as restore_graph:
        result = runner.invoke(migrate_cli.app, ["import", str(archive_path), "--yes"])

    assert result.exit_code == 0
    payload, org_id = restore_graph.call_args.args[:2]
    assert payload["organization_id"] == "org-xyz"
    assert org_id == "org-xyz"
    assert restore_graph.call_args.kwargs == {"clean": False}


def test_migrate_import_restores_postgres_and_graph(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, org_id="org-xyz")

    with (
        patch("sibyl.cli.migrate._restore_pg_sql") as restore_pg,
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True) as restore_graph,
    ):
        result = runner.invoke(
            migrate_cli.app,
            ["import", str(archive_path), "--yes", "--restore-database-dump"],
        )

    assert result.exit_code == 0
    restore_pg.assert_called_once_with("select 1;\n", False)
    payload, org_id = restore_graph.call_args.args[:2]
    assert payload["organization_id"] == "org-xyz"
    assert org_id == "org-xyz"
    assert restore_graph.call_args.kwargs == {"clean": False}


def test_migrate_import_warns_when_postgres_payload_is_skipped(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path)

    with patch("sibyl.cli.migrate._restore_graph_payload", return_value=True):
        result = runner.invoke(migrate_cli.app, ["import", str(archive_path), "--yes"])

    assert result.exit_code == 0
    assert "database dump sidecar" in result.output
    assert "restore is disabled" in result.output


def test_migrate_import_warns_when_auth_payload_is_skipped_in_postgres_mode(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, include_auth=True)

    with (
        patch.object(migrate_cli.settings, "auth_store", "postgres"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
    ):
        result = runner.invoke(migrate_cli.app, ["import", str(archive_path), "--yes"])

    assert result.exit_code == 0
    assert "Archive includes auth.json, but SIBYL_AUTH_STORE is not surreal" in result.output


def test_migrate_import_restores_auth_when_surreal_auth_store_is_enabled(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, include_auth=True)

    with (
        patch.object(migrate_cli.settings, "auth_store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate._restore_auth_payload", return_value=True) as restore_auth,
    ):
        result = runner.invoke(migrate_cli.app, ["import", str(archive_path), "--yes"])

    assert result.exit_code == 0
    payload = restore_auth.call_args.args[0]
    assert payload["row_counts"]["users"] == 1
    assert restore_auth.call_args.kwargs == {"clean": False}


def test_migrate_import_warns_when_content_payload_is_skipped_in_legacy_store(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, include_content=True)

    with (
        patch.object(migrate_cli.settings, "store", "legacy"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
    ):
        result = runner.invoke(migrate_cli.app, ["import", str(archive_path), "--yes"])

    assert result.exit_code == 0
    assert "Archive includes content.json, but SIBYL_STORE is not surreal" in result.output


def test_migrate_import_restores_content_when_surreal_store_is_enabled(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, include_content=True)

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate._restore_content_payload", return_value=True) as restore_content,
    ):
        result = runner.invoke(migrate_cli.app, ["import", str(archive_path), "--yes"])

    assert result.exit_code == 0
    payload = restore_content.call_args.args[0]
    assert payload["row_counts"]["document_chunks"] == 1
    assert restore_content.call_args.kwargs == {"clean": False}


def test_migrate_check_accepts_backup_all_directory_layout(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backup-all"
    backup_dir.mkdir()
    (backup_dir / "20260420_120000_sibyl_pg.sql").write_text("select 1;\n", encoding="utf-8")
    (backup_dir / "20260420_120000_sibyl_graph.json").write_text(
        json.dumps(
            {
                "version": "2.0",
                "created_at": "2026-04-19T20:00:00+00:00",
                "organization_id": "org-123",
                "entity_count": 1,
                "relationship_count": 0,
                "entities": [{"id": "entity-1"}],
                "relationships": [],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(migrate_cli.app, ["check", str(backup_dir)])

    assert result.exit_code == 0
    assert "Archive validation passed" in result.output


def test_migrate_verify_uses_runtime_verifier(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)
    verify_graph_archive = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            expected_entities=1,
            actual_entities=1,
            expected_relationships=0,
            actual_relationships=0,
            validated_entity_ids=["entity-1"],
            errors=[],
        )
    )

    with patch("sibyl.cli.migrate.verify_graph_archive", verify_graph_archive):
        result = runner.invoke(migrate_cli.app, ["verify", str(archive_path)])

    assert result.exit_code == 0
    assert "Archive verification passed" in result.output


def test_migrate_rehearse_runs_verify_and_baseline(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    manifest_path = tmp_path / "runtime-manifest.json"
    _write_graph_archive(archive_path)
    manifest_path.write_text('{"graph_fixture": {}}\n', encoding="utf-8")

    verify_graph_archive = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            expected_entities=1,
            actual_entities=1,
            expected_relationships=0,
            actual_relationships=0,
            validated_entity_ids=["entity-1"],
            errors=[],
        )
    )
    replay_all = AsyncMock(return_value=None)

    with (
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate.verify_graph_archive", verify_graph_archive),
        patch("sibyl.cli.migrate._replay_baseline", replay_all),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "rehearse",
                str(archive_path),
                "--yes",
                "--manifest-path",
                str(manifest_path),
            ],
        )

    assert result.exit_code == 0
    assert "Migration rehearsal passed" in result.output


def test_migrate_cutover_requires_surreal_store(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    with patch.object(migrate_cli.settings, "store", "legacy"):
        result = runner.invoke(
            migrate_cli.app,
            ["cutover", str(archive_path), "--dry-run", "--skip-baseline"],
        )

    assert result.exit_code == 1
    assert "SIBYL_STORE=surreal" in result.output


def test_migrate_cutover_dry_run_prints_plan(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    with patch.object(migrate_cli.settings, "store", "surreal"):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                "--dry-run",
                "--skip-baseline",
                "--run-bench-live-smoke",
                "--run-bench-live",
                "--reopen-writes",
            ],
        )

    assert result.exit_code == 0
    assert "Cutover plan:" in result.output
    assert "Import archive into the Surreal runtime" in result.output
    assert "Run bench-live artifact capture" in result.output
    assert "Reopen writes on SurrealDB" in result.output
    assert "Cutover dry run complete" in result.output


def test_migrate_cutover_requires_write_freeze_confirmation(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    with patch.object(migrate_cli.settings, "store", "surreal"):
        result = runner.invoke(
            migrate_cli.app,
            ["cutover", str(archive_path), "--yes", "--skip-baseline"],
        )

    assert result.exit_code == 1
    assert "--write-freeze-confirmed" in result.output


def test_migrate_cutover_leaves_writes_frozen_until_explicit_reopen(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    manifest_path = tmp_path / "runtime-manifest.json"
    _write_graph_archive(archive_path)
    manifest_path.write_text('{"graph_fixture": {}}\n', encoding="utf-8")

    verify_graph_archive = AsyncMock(return_value=_verify_result())
    replay_all = AsyncMock(return_value=None)

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate.verify_graph_archive", verify_graph_archive),
        patch("sibyl.cli.migrate._replay_baseline", replay_all),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                "--yes",
                "--write-freeze-confirmed",
                "--manifest-path",
                str(manifest_path),
            ],
        )

    assert result.exit_code == 0
    assert "Acceptance suite passed while writes remain frozen" in result.output
    assert "Rollback is still supported at this point" in result.output


def test_migrate_cutover_requires_ack_before_reopen(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    manifest_path = tmp_path / "runtime-manifest.json"
    _write_graph_archive(archive_path)
    manifest_path.write_text('{"graph_fixture": {}}\n', encoding="utf-8")

    verify_graph_archive = AsyncMock(return_value=_verify_result())
    replay_all = AsyncMock(return_value=None)

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate.verify_graph_archive", verify_graph_archive),
        patch("sibyl.cli.migrate._replay_baseline", replay_all),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                "--yes",
                "--write-freeze-confirmed",
                "--manifest-path",
                str(manifest_path),
                "--reopen-writes",
            ],
        )

    assert result.exit_code == 1
    assert "--acknowledge-no-instant-rollback" in result.output
