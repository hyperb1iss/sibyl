"""Tests for the migration archive CLI."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from sibyl.cli import migrate as migrate_cli
from sibyl_core.migrate.archive import GRAPH_FILENAME, build_manifest, write_archive

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
