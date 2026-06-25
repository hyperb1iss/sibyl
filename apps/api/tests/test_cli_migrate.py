"""Tests for the migration archive CLI."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from sibyl.cli import migrate as migrate_cli
from sibyl.cli.common import run_async
from sibyl_core.migrate.archive import (
    AUTH_FILENAME,
    CONTENT_FILENAME,
    GRAPH_FILENAME,
    build_manifest,
    graph_payload_from_archive,
    load_archive,
    write_archive,
)

runner = CliRunner()
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
ARCHIVE_FLAGS = ["--source-type", "surreal-archive", "--target-mode", "surreal"]


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


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
    }
    file_metadata: dict[str, dict[str, object]] = {
        GRAPH_FILENAME: {"kind": "graph", "entity_count": 1, "relationship_count": 0},
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


def test_migrate_cloud_env_writes_env_file_and_next_steps(tmp_path: Path) -> None:
    output = tmp_path / "sibyl-cloud.env"

    result = runner.invoke(
        migrate_cli.app,
        [
            "cloud-env",
            "--output",
            str(output),
            "--surreal-url",
            "wss://cloud.example.test/rpc",
            "--surreal-username",
            "root",
            "--surreal-password",
            "secret",
            "--public-url",
            "https://sibyl.example.test",
            "--canonical-org-id",
            "org-canonical",
        ],
    )

    assert result.exit_code == 0
    payload = output.read_text()
    assert 'SIBYL_SURREAL_URL="wss://cloud.example.test/rpc"' in payload
    assert 'SIBYL_PUBLIC_URL="https://sibyl.example.test"' in payload
    assert 'SIBYL_MIGRATION_CANONICAL_ORG_ID="org-canonical"' in payload
    assert "sibyld migrate export -o sibyl-$(hostname).tar.gz" in result.output
    assert "--canonical-org-id org-canonical" in result.output


def test_migrate_cloud_env_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    output = tmp_path / "sibyl-cloud.env"
    output.write_text("existing", encoding="utf-8")

    result = runner.invoke(migrate_cli.app, ["cloud-env", "--output", str(output)])

    assert result.exit_code == 1
    assert output.read_text(encoding="utf-8") == "existing"
    assert "already exists" in result.output


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


def test_migrate_merge_rejects_invalid_user_collision_policy(tmp_path: Path) -> None:
    source_archive = tmp_path / "source.tar.gz"
    _write_graph_archive(source_archive, org_id="org-a")

    result = runner.invoke(
        migrate_cli.app,
        [
            "merge",
            str(source_archive),
            "--canonical-org-id",
            "org-canonical",
            "--user-collision-policy",
            "email-ish",
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert "Invalid --user-collision-policy" in result.output
    assert "provider-or-email" in result.output


def test_migrate_consolidate_plan_prints_safe_flow() -> None:
    archive_path = Path("existing.tar.gz")
    output_path = Path("merged.tar.gz")
    work_dir = Path("work")

    result = runner.invoke(
        migrate_cli.app,
        [
            "consolidate",
            "--source",
            "local=org-local",
            "--source",
            "laptop=org-laptop",
            "--archive",
            str(archive_path),
            "--canonical-org-id",
            "org-canonical",
            "--canonical-org-name",
            "Stefanie Jane",
            "--canonical-org-slug",
            "stefanie-jane",
            "--target-host",
            "eternia",
            "--target-sudo",
            "--server-url",
            "https://sibyl.example.test",
            "--setup-cli",
            "--apply",
            "--plan",
            "--output",
            str(output_path),
            "--work-dir",
            str(work_dir),
        ],
    )

    output = _strip_ansi(result.output)
    assert result.exit_code == 0
    assert "sibyld migrate export" in output
    assert "--skip-auth" in output
    assert "ssh laptop sibyld migrate export" in output
    assert "sibyld migrate merge" in output
    assert "--canonical-org-id org-canonical" in output
    assert f"scp {output_path} eternia:/tmp/sibyl-consolidated.tar.gz" in output
    assert "sudo -n docker cp /tmp/sibyl-consolidated.tar.gz" in output
    assert "sudo -n docker compose --project-directory /opt/sibyl" in output
    assert "sibyld migrate import /tmp/sibyl-consolidated.tar.gz" in output
    assert "--target-mode surreal --skip-auth --yes --dry-run" in output
    assert "sibyld migrate verify /tmp/sibyl-consolidated.tar.gz" in output
    assert "sibyl init --remote https://sibyl.example.test --name remote --force" in output
    assert "sibyl auth login https://sibyl.example.test --context remote" in output


def test_migrate_consolidate_requires_sources_or_archives() -> None:
    result = runner.invoke(
        migrate_cli.app,
        ["consolidate", "--canonical-org-id", "org-canonical", "--plan"],
    )

    assert result.exit_code == 1
    assert "Pass at least one --source or --archive" in result.output


def test_migrate_consolidate_setup_cli_requires_login_context(tmp_path: Path) -> None:
    archive_path = tmp_path / "existing.tar.gz"

    result = runner.invoke(
        migrate_cli.app,
        [
            "consolidate",
            "--archive",
            str(archive_path),
            "--canonical-org-id",
            "org-canonical",
            "--setup-cli",
            "--plan",
        ],
    )

    assert result.exit_code == 1
    assert "--setup-cli requires --server-url" in result.output


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


def test_migrate_export_writes_graph_and_runtime_archive(tmp_path: Path) -> None:
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
    assert "Rerun the export with --org-id ORG_UUID" in result.output
    assert "--org-id" in result.output
    assert "ORG_UUID" in result.output
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
        result = runner.invoke(
            migrate_cli.app,
            ["import", str(archive_path), *ARCHIVE_FLAGS, "--yes"],
        )

    assert result.exit_code == 0
    payload, org_id = restore_graph.call_args.args[:2]
    assert payload["organization_id"] == "org-xyz"
    assert org_id == "org-xyz"
    assert restore_graph.call_args.kwargs == {"clean": False}


def test_migrate_import_requires_explicit_source_type_and_target_mode(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    with patch("sibyl.cli.migrate._restore_graph_payload", return_value=True) as restore_graph:
        result = runner.invoke(migrate_cli.app, ["import", str(archive_path), "--yes"])
        missing_target = runner.invoke(
            migrate_cli.app,
            [
                "import",
                str(archive_path),
                "--source-type",
                "surreal-archive",
                "--yes",
            ],
        )

    assert result.exit_code != 0
    assert "--source-type" in _strip_ansi(result.output)
    assert missing_target.exit_code != 0
    assert "--target-mode" in _strip_ansi(missing_target.output)
    restore_graph.assert_not_called()


def test_migrate_rehearse_and_cutover_require_explicit_policy_flags(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    command_args = [
        ("rehearse", ["--yes", "--skip-baseline"]),
        ("cutover", ["--dry-run", "--skip-baseline"]),
    ]

    for command, extra_args in command_args:
        result = runner.invoke(
            migrate_cli.app,
            [command, str(archive_path), *extra_args],
        )
        missing_target = runner.invoke(
            migrate_cli.app,
            [
                command,
                str(archive_path),
                "--source-type",
                "surreal-archive",
                *extra_args,
            ],
        )

        assert result.exit_code != 0
        assert "--source-type" in _strip_ansi(result.output)
        assert missing_target.exit_code != 0
        assert "--target-mode" in _strip_ansi(missing_target.output)


def test_migrate_import_dry_run_reports_restore_review_without_writes(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, include_auth=True, include_content=True)

    with (
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True) as restore_graph,
        patch("sibyl.cli.migrate._restore_auth_payload", return_value=True) as restore_auth,
        patch("sibyl.cli.migrate._restore_content_payload", return_value=True) as restore_content,
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "import",
                str(archive_path),
                "--dry-run",
                *ARCHIVE_FLAGS,
            ],
        )

    assert result.exit_code == 0
    assert "Archive restore review:" in result.output
    assert "declared source type: surreal-archive" in result.output
    assert "target mode: surreal" in result.output
    assert "Archive import dry run complete" in result.output
    restore_graph.assert_not_called()
    restore_auth.assert_not_called()
    restore_content.assert_not_called()


def test_migrate_import_dry_run_reports_unsupported_payloads(tmp_path: Path) -> None:
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
    files = {
        GRAPH_FILENAME: json.dumps(graph_payload).encode("utf-8"),
        "old-falkor-export.json": b"{}",
    }
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={
            GRAPH_FILENAME: {"kind": "graph", "entity_count": 1, "relationship_count": 0},
            "old-falkor-export.json": {"kind": "graph"},
        },
    )
    write_archive(archive_path, manifest=manifest, files=files)

    result = runner.invoke(
        migrate_cli.app,
        ["import", str(archive_path), *ARCHIVE_FLAGS, "--dry-run"],
    )

    assert result.exit_code == 0
    assert "Unsupported archive payloads will be ignored" in result.output
    assert "old-falkor-export.json" in result.output


def test_migrate_import_warns_when_auth_payload_restore_is_disabled(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, include_auth=True)

    with (
        patch.object(migrate_cli.settings, "auth_store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
    ):
        result = runner.invoke(
            migrate_cli.app,
            ["import", str(archive_path), *ARCHIVE_FLAGS, "--yes", "--skip-auth"],
        )

    assert result.exit_code == 0
    assert "Archive includes auth.json, but auth restore is disabled" in result.output


def test_migrate_import_restores_auth_when_surreal_auth_store_is_enabled(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, include_auth=True)

    with (
        patch.object(migrate_cli.settings, "auth_store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate._restore_auth_payload", return_value=True) as restore_auth,
    ):
        result = runner.invoke(
            migrate_cli.app,
            ["import", str(archive_path), *ARCHIVE_FLAGS, "--yes"],
        )

    assert result.exit_code == 0
    payload = restore_auth.call_args.args[0]
    assert payload["row_counts"]["users"] == 1
    assert restore_auth.call_args.kwargs == {"clean": False}


def test_migrate_import_restores_content_when_surreal_store_is_enabled(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, include_content=True)

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate._restore_content_payload", return_value=True) as restore_content,
    ):
        result = runner.invoke(
            migrate_cli.app,
            ["import", str(archive_path), *ARCHIVE_FLAGS, "--yes"],
        )

    assert result.exit_code == 0
    payload = restore_content.call_args.args[0]
    assert payload["row_counts"]["document_chunks"] == 1
    assert restore_content.call_args.kwargs == {"clean": False}


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


def test_migrate_auth_flow_runs_standalone_gate() -> None:
    auth_flow = AsyncMock(return_value=None)

    with patch("sibyl.cli.migrate._run_auth_flow_gate", auth_flow):
        result = runner.invoke(
            migrate_cli.app,
            [
                "auth-flow",
                "--base-url",
                "http://sibyl.test",
                "--auth-flow-email",
                "auth-flow@example.com",
                "--auth-flow-password",
                "auth-flow-password-secure-123!",
            ],
        )

    assert result.exit_code == 0
    assert "Auth flow harness passed" in result.output
    auth_flow.assert_awaited_once_with(
        base_url="http://sibyl.test",
        auth_flow_email="auth-flow@example.com",
        auth_flow_password="auth-flow-password-secure-123!",
        email_outbox_path=migrate_cli.DEFAULT_AUTH_FLOW_EMAIL_OUTBOX,
    )


def test_migrate_auth_flow_compare_runs_dual_store_gate(tmp_path: Path) -> None:
    auth_flow_compare = AsyncMock(return_value=None)
    postgres_outbox = tmp_path / "postgres-outbox.jsonl"
    surreal_outbox = tmp_path / "surreal-outbox.jsonl"

    with patch("sibyl.cli.migrate._run_auth_flow_compare", auth_flow_compare):
        result = runner.invoke(
            migrate_cli.app,
            [
                "auth-flow-compare",
                "--postgres-base-url",
                "http://postgres.test",
                "--surreal-base-url",
                "http://surreal.test",
                "--postgres-auth-flow-email",
                "postgres-auth-flow@example.com",
                "--surreal-auth-flow-email",
                "surreal-auth-flow@example.com",
                "--auth-flow-password",
                "auth-flow-password-secure-123!",
                "--postgres-email-outbox-path",
                str(postgres_outbox),
                "--surreal-email-outbox-path",
                str(surreal_outbox),
            ],
        )

    assert result.exit_code == 0
    assert "Auth flow semantic comparison passed" in result.output
    auth_flow_compare.assert_awaited_once_with(
        postgres_base_url="http://postgres.test",
        surreal_base_url="http://surreal.test",
        postgres_auth_flow_email="postgres-auth-flow@example.com",
        surreal_auth_flow_email="surreal-auth-flow@example.com",
        auth_flow_password="auth-flow-password-secure-123!",
        postgres_email_outbox_path=postgres_outbox,
        surreal_email_outbox_path=surreal_outbox,
    )


def test_migrate_auth_flow_compare_rejects_same_base_url() -> None:
    auth_flow_compare = AsyncMock(return_value=None)

    with patch("sibyl.cli.migrate._run_auth_flow_compare", auth_flow_compare):
        result = runner.invoke(
            migrate_cli.app,
            [
                "auth-flow-compare",
                "--postgres-base-url",
                "http://LOCALHOST:3334/",
                "--surreal-base-url",
                "http://localhost:3334",
            ],
        )

    assert result.exit_code == 1
    assert "auth-flow-compare requires distinct Postgres and Surreal base URLs" in result.output
    auth_flow_compare.assert_not_awaited()


def test_migrate_auth_flow_compare_allows_same_base_url_for_debug() -> None:
    auth_flow_compare = AsyncMock(return_value=None)

    with patch("sibyl.cli.migrate._run_auth_flow_compare", auth_flow_compare):
        result = runner.invoke(
            migrate_cli.app,
            [
                "auth-flow-compare",
                "--postgres-base-url",
                "http://localhost:3334",
                "--surreal-base-url",
                "http://localhost:3334/",
                "--allow-same-base-url",
            ],
        )

    assert result.exit_code == 0
    assert "Auth flow semantic comparison passed" in result.output
    auth_flow_compare.assert_awaited_once()


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
    auth_flow = AsyncMock(return_value=None)

    with (
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate.verify_graph_archive", verify_graph_archive),
        patch("sibyl.cli.migrate._replay_baseline", replay_all),
        patch("sibyl.cli.migrate._run_auth_flow_gate", auth_flow),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "rehearse",
                str(archive_path),
                *ARCHIVE_FLAGS,
                "--yes",
                "--manifest-path",
                str(manifest_path),
            ],
        )

    assert result.exit_code == 0
    assert "Migration rehearsal passed" in result.output
    auth_flow.assert_awaited_once_with(
        base_url=migrate_cli.DEFAULT_REHEARSAL_BASE_URL,
        auth_flow_email="",
        auth_flow_password=migrate_cli.DEFAULT_AUTH_FLOW_PASSWORD,
        email_outbox_path=migrate_cli.DEFAULT_AUTH_FLOW_EMAIL_OUTBOX,
    )


def test_migrate_rehearse_passes_auth_flow_options(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    verify_graph_archive = AsyncMock(return_value=_verify_result())
    auth_flow = AsyncMock(return_value=None)

    with (
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate.verify_graph_archive", verify_graph_archive),
        patch("sibyl.cli.migrate._run_auth_flow_gate", auth_flow),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "rehearse",
                str(archive_path),
                *ARCHIVE_FLAGS,
                "--yes",
                "--skip-baseline",
                "--base-url",
                "http://sibyl.test",
                "--auth-flow-email",
                "auth-flow@example.com",
                "--auth-flow-password",
                "auth-flow-password-secure-123!",
                "--email-outbox-path",
                str(tmp_path / "email-outbox.jsonl"),
            ],
        )

    assert result.exit_code == 0
    auth_flow.assert_awaited_once_with(
        base_url="http://sibyl.test",
        auth_flow_email="auth-flow@example.com",
        auth_flow_password="auth-flow-password-secure-123!",
        email_outbox_path=tmp_path / "email-outbox.jsonl",
    )


def test_migrate_cutover_requires_surreal_store(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    with patch.object(migrate_cli.settings, "store", "legacy"):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                *ARCHIVE_FLAGS,
                "--dry-run",
                "--skip-baseline",
            ],
        )

    assert result.exit_code == 1
    assert "SIBYL_STORE=surreal" in result.output


def test_migrate_cutover_ignores_removed_postgres_auth_store(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_full_archive(archive_path, include_auth=True)

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch.object(migrate_cli.settings, "auth_store", "postgres"),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                *ARCHIVE_FLAGS,
                "--dry-run",
                "--skip-baseline",
            ],
        )

    assert result.exit_code == 0
    assert "Cutover dry run complete" in result.output


def test_migrate_cutover_dry_run_prints_plan(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    with patch.object(migrate_cli.settings, "store", "surreal"):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                *ARCHIVE_FLAGS,
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
    assert "Run auth-flow acceptance harness" in result.output
    assert "Run bench-live artifact capture" in result.output
    assert "Operator freezes legacy auth/RBAC writes before reopening" in result.output
    assert "Reopen writes on SurrealDB" in result.output
    assert "Cutover dry run complete" in result.output


def test_migrate_cutover_requires_write_freeze_confirmation(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    _write_graph_archive(archive_path)

    with patch.object(migrate_cli.settings, "store", "surreal"):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                *ARCHIVE_FLAGS,
                "--yes",
                "--skip-baseline",
            ],
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
    auth_flow = AsyncMock(return_value=None)

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate.verify_graph_archive", verify_graph_archive),
        patch("sibyl.cli.migrate._replay_baseline", replay_all),
        patch("sibyl.cli.migrate._run_auth_flow_gate", auth_flow),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                *ARCHIVE_FLAGS,
                "--yes",
                "--write-freeze-confirmed",
                "--manifest-path",
                str(manifest_path),
            ],
        )

    assert result.exit_code == 0
    assert "Acceptance suite passed while writes remain frozen" in result.output
    assert "Rollback is still supported at this point" in result.output
    auth_flow.assert_awaited_once_with(
        base_url=migrate_cli.DEFAULT_REHEARSAL_BASE_URL,
        auth_flow_email="",
        auth_flow_password=migrate_cli.DEFAULT_AUTH_FLOW_PASSWORD,
        email_outbox_path=migrate_cli.DEFAULT_AUTH_FLOW_EMAIL_OUTBOX,
    )


def test_migrate_cutover_requires_ack_before_reopen(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    manifest_path = tmp_path / "runtime-manifest.json"
    _write_graph_archive(archive_path)
    manifest_path.write_text('{"graph_fixture": {}}\n', encoding="utf-8")

    verify_graph_archive = AsyncMock(return_value=_verify_result())
    replay_all = AsyncMock(return_value=None)
    auth_flow = AsyncMock(return_value=None)

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate.verify_graph_archive", verify_graph_archive),
        patch("sibyl.cli.migrate._replay_baseline", replay_all),
        patch("sibyl.cli.migrate._run_auth_flow_gate", auth_flow),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                *ARCHIVE_FLAGS,
                "--yes",
                "--write-freeze-confirmed",
                "--manifest-path",
                str(manifest_path),
                "--reopen-writes",
            ],
        )

    assert result.exit_code == 1
    assert "--acknowledge-no-instant-rollback" in result.output


def test_migrate_cutover_reopen_prints_rollback_boundary(tmp_path: Path) -> None:
    archive_path = tmp_path / "migration.tar.gz"
    manifest_path = tmp_path / "runtime-manifest.json"
    _write_graph_archive(archive_path)
    manifest_path.write_text('{"graph_fixture": {}}\n', encoding="utf-8")

    verify_graph_archive = AsyncMock(return_value=_verify_result())
    replay_all = AsyncMock(return_value=None)
    auth_flow = AsyncMock(return_value=None)

    with (
        patch.object(migrate_cli.settings, "store", "surreal"),
        patch.object(migrate_cli.settings, "auth_store", "surreal"),
        patch("sibyl.cli.migrate._restore_graph_payload", return_value=True),
        patch("sibyl.cli.migrate.verify_graph_archive", verify_graph_archive),
        patch("sibyl.cli.migrate._replay_baseline", replay_all),
        patch("sibyl.cli.migrate._run_auth_flow_gate", auth_flow),
    ):
        result = runner.invoke(
            migrate_cli.app,
            [
                "cutover",
                str(archive_path),
                *ARCHIVE_FLAGS,
                "--yes",
                "--write-freeze-confirmed",
                "--manifest-path",
                str(manifest_path),
                "--reopen-writes",
                "--acknowledge-no-instant-rollback",
            ],
        )

    assert result.exit_code == 0
    assert "Rollback is no longer promised once writes reopen on SurrealDB" in result.output
    assert "Writes may now be reopened on the Surreal runtime" in result.output


def test_migrate_collapse_epics_dry_run_then_apply(tmp_path: Path) -> None:
    """collapse-epics reports counts on dry run and only writes with --apply.

    Runs against an ephemeral embedded graph in a temp dir, never a live org.
    A file-backed embedded URL (not ``memory://``) is used deliberately: the
    Typer command runs each invocation in its own ``asyncio.run`` loop, and
    ``memory://`` hands out a fresh empty database per connection, so on-disk
    embedded storage is what lets the dry-run and --apply calls observe one
    consistent database across loops.
    """
    import uuid

    from sibyl_core.models.entities import EntityType
    from sibyl_core.models.tasks import Epic, EpicStatus, Task, TaskStatus
    from sibyl_core.services.graph import (
        EntityManager,
        GraphRuntime,
        RelationshipManager,
        SurrealGraphClient,
        prepare_graph_schema,
    )

    group_id = "00000000-0000-0000-0000-0000000000c1"
    epic_id = f"epic_{uuid.uuid4().hex[:8]}"
    child_id = f"task_{uuid.uuid4().hex[:8]}"
    graph_url = f"surrealkv://{tmp_path / 'collapse-epics.skv'}"

    def _new_client() -> SurrealGraphClient:
        return SurrealGraphClient(group_id=group_id, url=graph_url)

    async def _seed() -> None:
        client = _new_client()
        await client.connect()
        await prepare_graph_schema(client)
        manager = EntityManager(client, group_id=group_id)
        await manager.create_direct(
            Epic(
                id=epic_id,
                name="Launch",
                title="Launch",
                project_id="project-1",
                status=EpicStatus.IN_PROGRESS,
            )
        )
        await manager.create_direct(
            Task(
                id=child_id,
                name="Child",
                title="Child",
                status=TaskStatus.DOING,
                epic_id=epic_id,
            )
        )
        await client.close()

    async def _epic_type() -> EntityType:
        client = _new_client()
        await client.connect()
        try:
            entity = await EntityManager(client, group_id=group_id).get(epic_id)
            return entity.entity_type
        finally:
            await client.close()

    # Each command invocation gets a runtime bound to its own loop. The graph
    # state itself persists on disk between calls.
    async def _runtime(group: str, **_kwargs: object) -> GraphRuntime:
        client = _new_client()
        await client.connect()
        await prepare_graph_schema(client)
        return GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=group),
            relationship_manager=RelationshipManager(client, group_id=group),
        )

    run_async(_seed)()

    with patch(
        "sibyl_core.services.graph.get_surreal_graph_runtime",
        side_effect=_runtime,
    ):
        dry = runner.invoke(
            migrate_cli.app,
            ["collapse-epics", "--org-id", group_id],
        )
        assert dry.exit_code == 0, dry.output
        assert "DRY RUN" in dry.output
        assert "Would convert 1" in dry.output
        # Dry run wrote nothing: the epic is still an epic.
        assert run_async(_epic_type)() is EntityType.EPIC

        applied = runner.invoke(
            migrate_cli.app,
            ["collapse-epics", "--org-id", group_id, "--apply"],
        )
        assert applied.exit_code == 0, applied.output
        assert "Converted 1" in applied.output
        # Apply flipped the epic into a task in place.
        assert run_async(_epic_type)() is EntityType.TASK
