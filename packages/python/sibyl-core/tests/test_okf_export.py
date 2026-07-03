from __future__ import annotations

import json
from pathlib import Path

import pytest

from sibyl_core.export import (
    OKF_VERSION,
    build_okf_bundle_from_archive,
    build_okf_bundle_from_graph_payload,
    reconstruct_graph_payload_from_okf_bundle,
    validate_okf_bundle,
    write_okf_bundle,
)
from sibyl_core.migrate.archive import GRAPH_FILENAME, LoadedArchive, build_manifest


def _graph_payload() -> dict[str, object]:
    return {
        "version": "2.0",
        "created_at": "2026-07-03T12:00:00+00:00",
        "organization_id": "org-okf",
        "entity_count": 2,
        "relationship_count": 1,
        "episode_count": 1,
        "mention_count": 1,
        "entities": [
            {
                "id": "entity-alpha",
                "entity_type": "project",
                "name": "Alpha Project",
                "description": "Primary project memory.",
                "created_at": "2026-07-01T12:00:00+00:00",
            },
            {
                "id": "entity-beta",
                "entity_type": "topic",
                "name": "Beta Topic",
                "description": "Linked topic memory.",
                "created_at": "2026-07-02T12:00:00+00:00",
            },
        ],
        "relationships": [
            {
                "id": "rel-alpha-beta",
                "source_id": "entity-alpha",
                "target_id": "entity-beta",
                "relationship_type": "RELATED_TO",
                "weight": 0.75,
                "created_at": "2026-07-03T12:00:00+00:00",
            }
        ],
        "episodes": [
            {
                "uuid": "episode-alpha",
                "name": "Alpha episode",
                "content": "Alpha evidence from a session.",
                "created_at": "2026-07-03T12:00:00+00:00",
            }
        ],
        "mentions": [
            {
                "uuid": "mention-alpha",
                "source_node_uuid": "episode-alpha",
                "target_node_uuid": "entity-alpha",
                "created_at": "2026-07-03T12:00:00+00:00",
            }
        ],
    }


def _frontmatter(content: str) -> dict[str, object]:
    assert content.startswith("---\n")
    _, payload, _body = content.split("---", 2)
    loaded = json.loads(payload)
    assert isinstance(loaded, dict)
    return loaded


def test_okf_export_builds_valid_concepts_with_extension_edges() -> None:
    bundle = build_okf_bundle_from_graph_payload(_graph_payload())

    assert set(bundle.files) >= {
        "index.md",
        "log.md",
        "sibyl-graph.md",
        "entities/entity-alpha.md",
        "entities/entity-beta.md",
        "relationships/rel-alpha-beta.md",
        "episodes/episode-alpha.md",
        "mentions/mention-alpha.md",
    }
    assert bundle.files["index.md"].startswith("# Sibyl OKF Export\n")
    assert "okf_version" not in bundle.files["index.md"]

    entity = _frontmatter(bundle.files["entities/entity-alpha.md"])
    assert entity["type"] == "Sibyl Project Entity"
    assert entity["okf_version"] == OKF_VERSION
    assert entity["sibyl_kind"] == "entity"
    assert entity["sibyl_id"] == "entity-alpha"
    assert entity["sibyl_entity_type"] == "project"
    assert entity["edges"] == [
        {
            "target": "entity-beta",
            "target_path": "/entities/entity-beta.md",
            "type": "RELATED_TO",
            "weight": 0.75,
        }
    ]
    assert "[entity-beta](/entities/entity-beta.md)" in bundle.files["entities/entity-alpha.md"]

    relationship = _frontmatter(bundle.files["relationships/rel-alpha-beta.md"])
    assert relationship["edges"] == [
        {
            "source": "entity-alpha",
            "target": "entity-beta",
            "type": "RELATED_TO",
            "weight": 0.75,
        }
    ]


def test_okf_export_is_byte_stable_and_reconstructs_graph_payload(tmp_path: Path) -> None:
    payload = _graph_payload()
    first = build_okf_bundle_from_graph_payload(payload)
    second = build_okf_bundle_from_graph_payload(payload)

    assert first.files == second.files

    output = tmp_path / "okf"
    write_okf_bundle(first, output)

    assert validate_okf_bundle(output) == []
    assert reconstruct_graph_payload_from_okf_bundle(output) == payload


def test_okf_export_from_archive_requires_graph_payload() -> None:
    graph_bytes = json.dumps(_graph_payload()).encode("utf-8")
    archive = LoadedArchive(
        source=Path("memory.tar.gz"),
        manifest=build_manifest(
            organization_id="org-okf",
            source_store="surreal",
            files={GRAPH_FILENAME: graph_bytes},
        ),
        files={GRAPH_FILENAME: graph_bytes},
    )

    bundle = build_okf_bundle_from_archive(archive)

    assert "entities/entity-alpha.md" in bundle.files


def test_okf_export_rejects_archive_without_graph_payload() -> None:
    archive = LoadedArchive(
        source=Path("memory.tar.gz"),
        manifest=build_manifest(
            organization_id="org-okf",
            source_store="surreal",
            files={},
        ),
        files={},
    )

    with pytest.raises(ValueError, match=r"missing graph\.json"):
        build_okf_bundle_from_archive(archive)


def test_okf_validation_rejects_missing_type_and_missing_graph_metadata(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bad-okf"
    bundle_dir.mkdir()
    (bundle_dir / "concept.md").write_text(
        "---\n{}\n---\n\n# Missing type\n",
        encoding="utf-8",
    )

    assert validate_okf_bundle(bundle_dir) == [
        "concept.md: missing required OKF type",
        "OKF bundle is missing sibyl graph metadata",
    ]
