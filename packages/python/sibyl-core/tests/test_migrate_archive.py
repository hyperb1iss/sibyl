from __future__ import annotations

import json
from pathlib import Path

import pytest

from sibyl_core.migrate.archive import (
    AUTH_FILENAME,
    CONTENT_FILENAME,
    GRAPH_FILENAME,
    MANIFEST_FILENAME,
    auth_payload_from_archive,
    build_manifest,
    content_payload_from_archive,
    effective_graph_counts,
    graph_payload_from_archive,
    load_archive,
    normalize_mention_payloads,
    normalize_relationship_payloads,
    validate_archive,
    write_archive,
)
from sibyl_core.migrate.legacy_graph_archive import record_id
from sibyl_core.migrate.verify import verify_graph_archive


def _graph_bytes(
    entity_count: int = 2,
    relationship_count: int = 1,
    episode_count: int = 0,
    mention_count: int = 0,
) -> bytes:
    return json.dumps(
        {
            "version": "2.0",
            "created_at": "2026-04-19T20:00:00+00:00",
            "organization_id": "org-123",
            "entity_count": entity_count,
            "relationship_count": relationship_count,
            "episode_count": episode_count,
            "mention_count": mention_count,
            "entities": [{"id": "entity-1"}, {"id": "entity-2"}][:entity_count],
            "relationships": [{"id": "rel-1"}][:relationship_count],
            "episodes": [{"uuid": "episode-1"}][:episode_count],
            "mentions": [{"uuid": "mention-1"}][:mention_count],
        }
    ).encode("utf-8")


def _legacy_graph_payload() -> dict[str, object]:
    return {
        "version": "2.0",
        "created_at": "2026-04-19T20:00:00+00:00",
        "organization_id": "org-123",
        "entity_count": 1,
        "relationship_count": 0,
        "episode_count": 1,
        "mention_count": 1,
        "entities": [{"id": "entity-1", "entity_type": "topic", "name": "Legacy entity"}],
        "relationships": [],
        "episodes": [
            {
                "uuid": "episode-1",
                "name": "Legacy episode",
                "source": "message",
                "source_description": "chat",
                "content": "legacy memory payload",
                "labels": ["Episodic"],
                "group_id": "org-123",
                "created_at": "2026-04-19T20:00:00+00:00",
                "valid_at": "2026-04-19T20:00:00+00:00",
                "entity_edges": [],
            }
        ],
        "mentions": [
            {
                "uuid": "mention-1",
                "group_id": "org-123",
                "source_node_uuid": "episode-1",
                "target_node_uuid": "entity-1",
                "created_at": "2026-04-19T20:00:00+00:00",
            }
        ],
    }


def _legacy_graph_bytes() -> bytes:
    return json.dumps(_legacy_graph_payload()).encode("utf-8")


def _auth_bytes(*, user_rows: int = 1) -> bytes:
    return json.dumps(
        {
            "version": "1.0",
            "created_at": "2026-04-21T02:00:00+00:00",
            "tables": {
                "users": [
                    {"id": f"user-{index}", "email": f"user{index}@example.com"}
                    for index in range(user_rows)
                ],
                "organizations": [],
            },
            "row_counts": {
                "users": user_rows,
                "organizations": 0,
            },
            "total_rows": user_rows,
        }
    ).encode("utf-8")


def _content_bytes(*, chunk_rows: int = 1) -> bytes:
    return json.dumps(
        {
            "version": "1.0",
            "created_at": "2026-04-21T03:00:00+00:00",
            "tables": {
                "crawl_sources": [{"id": "source-1", "organization_id": "org-123", "name": "Docs"}],
                "crawled_documents": [
                    {"id": "document-1", "source_id": "source-1", "title": "Page"}
                ],
                "document_chunks": [
                    {"id": f"chunk-{index}", "document_id": "document-1"}
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
    ).encode("utf-8")


def test_archive_round_trip_preserves_manifest_and_payloads(tmp_path: Path) -> None:
    files = {
        AUTH_FILENAME: _auth_bytes(),
        CONTENT_FILENAME: _content_bytes(),
        GRAPH_FILENAME: _graph_bytes(),
    }
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={
            AUTH_FILENAME: {"kind": "auth", "table_count": 2, "total_rows": 1},
            CONTENT_FILENAME: {"kind": "content", "table_count": 7, "total_rows": 3},
            GRAPH_FILENAME: {"kind": "graph", "entity_count": 2, "relationship_count": 1},
        },
    )
    archive_path = tmp_path / "migration.tar.gz"

    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    assert validate_archive(loaded) == []
    assert loaded.manifest.organization_id == "org-123"
    assert loaded.manifest.source_store == "legacy"
    assert graph_payload_from_archive(loaded)["entity_count"] == 2
    assert auth_payload_from_archive(loaded)["row_counts"]["users"] == 1
    assert content_payload_from_archive(loaded)["row_counts"]["document_chunks"] == 1


def test_legacy_graph_archive_preserves_episode_and_mentions_records(tmp_path: Path) -> None:
    files = {GRAPH_FILENAME: _legacy_graph_bytes()}
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={GRAPH_FILENAME: {"kind": "graph"}},
    )
    archive_path = tmp_path / "legacy-migration.tar.gz"

    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    graph_payload = graph_payload_from_archive(loaded)
    assert graph_payload is not None
    assert validate_archive(loaded) == []
    assert graph_payload["episodes"] == _legacy_graph_payload()["episodes"]
    assert graph_payload["mentions"] == _legacy_graph_payload()["mentions"]
    assert normalize_mention_payloads(graph_payload["mentions"]) == graph_payload["mentions"]
    assert effective_graph_counts(graph_payload) == {
        "entity_count": 1,
        "relationship_count": 0,
        "episode_count": 1,
        "mention_count": 1,
    }


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


def test_validate_archive_detects_graph_count_drift(tmp_path: Path) -> None:
    files = {GRAPH_FILENAME: _graph_bytes(entity_count=2, relationship_count=1)}
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={GRAPH_FILENAME: {"kind": "graph"}},
    )
    archive_path = tmp_path / "migration.tar.gz"
    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    graph_payload = json.loads(loaded.files[GRAPH_FILENAME].decode("utf-8"))
    graph_payload["entity_count"] = 9
    loaded = loaded.__class__(
        source=loaded.source,
        manifest=build_manifest(
            organization_id="org-123",
            source_store="legacy",
            files={GRAPH_FILENAME: json.dumps(graph_payload).encode("utf-8")},
            file_metadata={GRAPH_FILENAME: {"kind": "graph"}},
        ),
        files={GRAPH_FILENAME: json.dumps(graph_payload).encode("utf-8")},
    )

    errors = validate_archive(loaded)

    assert errors == ["graph.json entity_count mismatch: declared 9, found 2 entities"]


def test_validate_archive_detects_episode_and_mention_count_drift(tmp_path: Path) -> None:
    files = {GRAPH_FILENAME: _graph_bytes(episode_count=1, mention_count=1)}
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={GRAPH_FILENAME: {"kind": "graph"}},
    )
    archive_path = tmp_path / "migration.tar.gz"
    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    graph_payload = json.loads(loaded.files[GRAPH_FILENAME].decode("utf-8"))
    graph_payload["episode_count"] = 2
    graph_payload["mention_count"] = 3
    mutated = json.dumps(graph_payload).encode("utf-8")
    loaded = loaded.__class__(
        source=loaded.source,
        manifest=build_manifest(
            organization_id="org-123",
            source_store="legacy",
            files={GRAPH_FILENAME: mutated},
            file_metadata={GRAPH_FILENAME: {"kind": "graph"}},
        ),
        files={GRAPH_FILENAME: mutated},
    )

    errors = validate_archive(loaded)

    assert errors == [
        "graph.json episode_count mismatch: declared 2, found 1 episodes",
        "graph.json mention_count mismatch: declared 3, found 1 mentions",
    ]


def test_validate_archive_detects_graph_org_mismatch(tmp_path: Path) -> None:
    files = {GRAPH_FILENAME: _graph_bytes(entity_count=2, relationship_count=1)}
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={GRAPH_FILENAME: {"kind": "graph"}},
    )
    archive_path = tmp_path / "migration.tar.gz"
    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    graph_payload = json.loads(loaded.files[GRAPH_FILENAME].decode("utf-8"))
    graph_payload["organization_id"] = "other-org"
    loaded = loaded.__class__(
        source=loaded.source,
        manifest=build_manifest(
            organization_id="org-123",
            source_store="legacy",
            files={GRAPH_FILENAME: json.dumps(graph_payload).encode("utf-8")},
            file_metadata={GRAPH_FILENAME: {"kind": "graph"}},
        ),
        files={GRAPH_FILENAME: json.dumps(graph_payload).encode("utf-8")},
    )

    errors = validate_archive(loaded)

    assert errors == ["graph.json organization_id mismatch: manifest org-123, payload other-org"]


def test_validate_archive_detects_auth_row_count_drift(tmp_path: Path) -> None:
    files = {AUTH_FILENAME: _auth_bytes(user_rows=1)}
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={AUTH_FILENAME: {"kind": "auth"}},
    )
    archive_path = tmp_path / "migration.tar.gz"
    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    auth_payload = auth_payload_from_archive(loaded)
    assert auth_payload is not None
    auth_payload["row_counts"]["users"] = 9
    mutated = json.dumps(auth_payload).encode("utf-8")
    loaded = loaded.__class__(
        source=loaded.source,
        manifest=build_manifest(
            organization_id="org-123",
            source_store="legacy",
            files={AUTH_FILENAME: mutated},
            file_metadata={AUTH_FILENAME: {"kind": "auth"}},
        ),
        files={AUTH_FILENAME: mutated},
    )

    errors = validate_archive(loaded)

    assert errors == ["auth.json users row_count mismatch: declared 9, found 1 rows"]


def test_validate_archive_detects_auth_total_row_drift(tmp_path: Path) -> None:
    files = {AUTH_FILENAME: _auth_bytes(user_rows=1)}
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={AUTH_FILENAME: {"kind": "auth"}},
    )
    archive_path = tmp_path / "migration.tar.gz"
    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    auth_payload = auth_payload_from_archive(loaded)
    assert auth_payload is not None
    auth_payload["total_rows"] = 9
    mutated = json.dumps(auth_payload).encode("utf-8")
    loaded = loaded.__class__(
        source=loaded.source,
        manifest=build_manifest(
            organization_id="org-123",
            source_store="legacy",
            files={AUTH_FILENAME: mutated},
            file_metadata={AUTH_FILENAME: {"kind": "auth"}},
        ),
        files={AUTH_FILENAME: mutated},
    )

    errors = validate_archive(loaded)

    assert errors == ["auth.json total_rows mismatch: declared 9, found 1 rows"]


def test_validate_archive_detects_content_row_count_drift(tmp_path: Path) -> None:
    files = {CONTENT_FILENAME: _content_bytes(chunk_rows=1)}
    manifest = build_manifest(
        organization_id="org-123",
        source_store="legacy",
        files=files,
        file_metadata={CONTENT_FILENAME: {"kind": "content"}},
    )
    archive_path = tmp_path / "migration.tar.gz"
    write_archive(archive_path, manifest=manifest, files=files)
    loaded = load_archive(archive_path)

    content_payload = content_payload_from_archive(loaded)
    assert content_payload is not None
    content_payload["row_counts"]["document_chunks"] = 9
    mutated = json.dumps(content_payload).encode("utf-8")
    loaded = loaded.__class__(
        source=loaded.source,
        manifest=build_manifest(
            organization_id="org-123",
            source_store="legacy",
            files={CONTENT_FILENAME: mutated},
            file_metadata={CONTENT_FILENAME: {"kind": "content"}},
        ),
        files={CONTENT_FILENAME: mutated},
    )

    errors = validate_archive(loaded)

    assert errors == ["content.json document_chunks row_count mismatch: declared 9, found 1 rows"]


def test_effective_graph_counts_normalize_duplicate_edges() -> None:
    graph_payload = {
        "entity_count": 2,
        "relationship_count": 3,
        "episode_count": 1,
        "mention_count": 3,
        "entities": [{"id": "entity-1"}, {"id": "entity-2"}],
        "episodes": [{"uuid": "episode-1"}],
        "relationships": [
            {
                "id": "rel-1",
                "source_id": "entity-1",
                "relationship_type": "related_to",
                "target_id": "entity-2",
            },
            {
                "id": "rel-2",
                "source_id": "entity-1",
                "relationship_type": "related_to",
                "target_id": "entity-2",
            },
            {
                "id": "rel-1",
                "source_id": "entity-2",
                "relationship_type": "depends_on",
                "target_id": "entity-1",
            },
        ],
        "mentions": [
            {"uuid": "mention-1", "source_id": "episode-1", "target_id": "entity-1"},
            {"uuid": "mention-2", "source_id": "episode-1", "target_id": "entity-2"},
            {"uuid": "mention-1", "source_id": "episode-1", "target_id": "entity-2"},
        ],
    }

    assert len(normalize_relationship_payloads(graph_payload["relationships"])) == 1
    assert len(normalize_mention_payloads(graph_payload["mentions"])) == 2
    assert effective_graph_counts(graph_payload) == {
        "entity_count": 2,
        "relationship_count": 1,
        "episode_count": 1,
        "mention_count": 2,
    }


@pytest.mark.asyncio
async def test_legacy_graph_archive_record_id_rejects_unknown_tables() -> None:
    class FakeClient:
        async def execute_query(self, query: str, **params: object) -> object:
            raise AssertionError("unsupported table names must not reach Surreal")

    with pytest.raises(ValueError, match="Unsupported backup record table"):
        await record_id(FakeClient(), "entity;DELETE", "entity-1")


@pytest.mark.asyncio
async def test_verify_graph_archive_checks_counts_and_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {GRAPH_FILENAME: _legacy_graph_bytes()}
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

    class FakeClient:
        async def execute_query(self, query: str, **params: object) -> object:
            # verify.py queries the episode table by uuid to confirm presence;
            # return a non-empty row so sampled episodes are validated.
            if "FROM episode" in query:
                return [{"uuid": params.get("uuid", ""), "group_id": params.get("group_id", "")}]
            return []

    class FakeRuntime:
        entity_manager = FakeEntityManager()
        client = FakeClient()

    class FakeBackup:
        success = True
        entity_count = 1
        relationship_count = 0
        episode_count = 1
        mention_count = 1
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
    assert result.expected_entities == 1
    assert result.actual_entities == 1
    assert result.expected_episodes == 1
    assert result.actual_episodes == 1
    assert result.expected_mentions == 1
    assert result.actual_mentions == 1
    assert result.validated_entity_ids == ["entity-1"]
    assert result.validated_episode_ids == ["episode-1"]
