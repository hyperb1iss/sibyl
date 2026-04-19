from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from sibyl_core.migrate.archive import (
    GRAPH_FILENAME,
    LEGACY_METADATA_FILENAME,
    MANIFEST_FILENAME,
    POSTGRES_FILENAME,
    build_manifest,
    graph_payload_from_archive,
    load_archive,
    validate_archive,
    write_archive,
)
from sibyl_core.migrate.verify import verify_graph_archive


def _graph_bytes(entity_count: int = 2, relationship_count: int = 1) -> bytes:
    return json.dumps(
        {
            "version": "2.0",
            "created_at": "2026-04-19T20:00:00+00:00",
            "organization_id": "org-123",
            "entity_count": entity_count,
            "relationship_count": relationship_count,
            "entities": [{"id": "entity-1"}, {"id": "entity-2"}][:entity_count],
            "relationships": [{"id": "rel-1"}][:relationship_count],
        }
    ).encode("utf-8")


def test_archive_round_trip_preserves_manifest_and_payloads(tmp_path: Path) -> None:
    files = {
        GRAPH_FILENAME: _graph_bytes(),
        POSTGRES_FILENAME: b"select 1;\n",
    }
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={
            GRAPH_FILENAME: {"kind": "graph", "entity_count": 2, "relationship_count": 1},
            POSTGRES_FILENAME: {"kind": "postgres"},
        },
    )
    archive_path = tmp_path / "migration.tar.gz"

    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    assert validate_archive(loaded) == []
    assert loaded.manifest.organization_id == "org-123"
    assert loaded.manifest.source_store == "legacy"
    assert graph_payload_from_archive(loaded)["entity_count"] == 2


def test_validate_archive_detects_checksum_mismatch(tmp_path: Path) -> None:
    source_files = {GRAPH_FILENAME: _graph_bytes()}
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=source_files,
        file_metadata={GRAPH_FILENAME: {"kind": "graph"}},
    )

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest.to_dict(), indent=2),
        encoding="utf-8",
    )
    (archive_dir / GRAPH_FILENAME).write_text('{"tampered": true}\n', encoding="utf-8")

    loaded = load_archive(archive_dir)
    errors = validate_archive(loaded)

    assert len(errors) == 2
    assert "checksum mismatch" in errors[0]
    assert "size mismatch" in errors[1]


def test_load_archive_supports_legacy_backup_metadata(tmp_path: Path) -> None:
    archive_path = tmp_path / "legacy.tar.gz"
    graph_bytes = _graph_bytes()
    graph_sha = __import__("hashlib").sha256(graph_bytes).hexdigest()
    metadata = {
        "version": "2.0",
        "created_at": "2026-04-19T20:30:00+00:00",
        "organization_id": "org-123",
        "files": {GRAPH_FILENAME: graph_sha},
    }

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / LEGACY_METADATA_FILENAME).write_text(json.dumps(metadata), encoding="utf-8")
    (legacy_dir / GRAPH_FILENAME).write_bytes(graph_bytes)
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(legacy_dir / LEGACY_METADATA_FILENAME, arcname=LEGACY_METADATA_FILENAME)
        tar.add(legacy_dir / GRAPH_FILENAME, arcname=GRAPH_FILENAME)

    loaded = load_archive(archive_path)

    assert validate_archive(loaded) == []
    assert loaded.manifest.organization_id == "org-123"
    assert loaded.manifest.source_store == "legacy"


@pytest.mark.asyncio
async def test_verify_graph_archive_checks_counts_and_samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {GRAPH_FILENAME: _graph_bytes()}
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={GRAPH_FILENAME: {"kind": "graph"}},
    )
    archive_path = tmp_path / "migration.tar.gz"
    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    class FakeEntityManager:
        async def get(self, entity_id: str) -> object | None:
            return {"id": entity_id}

    class FakeRuntime:
        entity_manager = FakeEntityManager()

    class FakeBackup:
        success = True
        entity_count = 2
        relationship_count = 1
        message = "ok"

    async def fake_create_backup(*, organization_id: str):
        assert organization_id == "org-123"
        return FakeBackup()

    async def fake_get_graph_runtime(group_id: str):
        assert group_id == "org-123"
        return FakeRuntime()

    monkeypatch.setattr("sibyl_core.migrate.verify.create_backup", fake_create_backup)
    monkeypatch.setattr("sibyl_core.migrate.verify.get_graph_runtime", fake_get_graph_runtime)

    result = await verify_graph_archive(loaded, organization_id="org-123")

    assert result.success is True
    assert result.expected_entities == 2
    assert result.actual_entities == 2
    assert result.validated_entity_ids == ["entity-1", "entity-2"]
