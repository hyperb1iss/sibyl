"""Tests for the migration archive CLI."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from sibyl.cli import migrate as migrate_cli
from sibyl_core.migrate.archive import (
    GRAPH_FILENAME,
    POSTGRES_FILENAME,
    build_manifest,
    load_archive,
    write_archive,
)

runner = CliRunner()


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
        file_metadata={GRAPH_FILENAME: {"kind": "graph", "entity_count": 1, "relationship_count": 0}},
    )
    write_archive(path, manifest=manifest, files=files)


def _write_full_archive(path: Path, *, org_id: str = "org-123") -> None:
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
    manifest = build_manifest(
        organization_id=org_id,
        source_store="legacy",
        files=files,
        file_metadata={
            GRAPH_FILENAME: {"kind": "graph", "entity_count": 1, "relationship_count": 0},
            POSTGRES_FILENAME: {"kind": "postgres"},
        },
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
                "--no-include-postgres",
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
        patch(
            "sibyl.cli.migrate._load_graph_export",
            return_value=(graph_payload, json.dumps(graph_payload).encode("utf-8")),
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
    assert POSTGRES_FILENAME in loaded.files
    assert GRAPH_FILENAME in loaded.files


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
            ["import", str(archive_path), "--yes", "--restore-postgres"],
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
    assert "Archive includes postgres.sql" in result.output


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
