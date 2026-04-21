"""Tests for database CLI graph restore compatibility."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl.cli import db as db_cli

runner = CliRunner()


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
        patch("sibyl.cli.db._prepare_graph_runtime", side_effect=AssertionError("sync helper should not be used")),
        patch("sibyl.cli.db._prepare_graph_runtime_async", prepare),
        patch("sibyl_core.tools.admin.restore_backup", restore_backup),
    ):
        result = runner.invoke(
            db_cli.app,
            ["restore", str(graph_file), "--org-id", "org-123", "--yes"],
        )

    assert result.exit_code == 0
    prepare.assert_awaited_once_with("org-123", clean=False)


def test_restore_graph_payload_prepares_runtime_in_same_async_flow() -> None:
    prepare = AsyncMock()
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

    with (
        patch("sibyl.cli.db._prepare_graph_runtime_async", prepare),
        patch("sibyl_core.tools.admin.restore_backup", restore_backup),
    ):
        result = db_cli._restore_graph_payload(
            {
                "entities": [{"id": "entity-1"}],
                "relationships": [],
            },
            "org-123",
            clean=True,
        )

    assert result is True
    prepare.assert_awaited_once_with("org-123", clean=True)


def test_prepare_graph_runtime_surreal_clears_rows_without_bootstrapping_schema(monkeypatch) -> None:
    driver = MagicMock()
    driver.graph_ops = SimpleNamespace(clear_data=AsyncMock())
    driver.build_indices_and_constraints = AsyncMock()

    client = MagicMock()
    client.get_org_driver.return_value = driver

    monkeypatch.setattr("sibyl.config.settings.store", "surreal")

    with patch("sibyl_core.graph.client.get_graph_client", AsyncMock(return_value=client)):
        db_cli._prepare_graph_runtime("org-123", clean=True)

    client.get_org_driver.assert_called_once_with("org-123")
    driver.graph_ops.clear_data.assert_awaited_once_with(driver, group_ids=["org-123"])
    driver.build_indices_and_constraints.assert_not_called()
