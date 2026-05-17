from __future__ import annotations

import json
from pathlib import Path

from sibyl_core.migrate import (
    AUTH_FILENAME,
    CONTENT_FILENAME,
    GRAPH_FILENAME,
    ArchiveMergeOptions,
    LoadedArchive,
    build_manifest,
    content_payload_from_archive,
    graph_payload_from_archive,
    load_archive,
    merge_archives,
    validate_archive,
    write_archive,
)

CANONICAL_ORG_ID = "org-canonical"


def _archive(
    org_id: str,
    *,
    graph: dict[str, object] | None = None,
    auth: dict[str, object] | None = None,
    content: dict[str, object] | None = None,
) -> LoadedArchive:
    files: dict[str, bytes] = {}
    file_metadata: dict[str, dict[str, object]] = {}

    if graph is not None:
        files[GRAPH_FILENAME] = json.dumps(graph).encode("utf-8")
        file_metadata[GRAPH_FILENAME] = {"kind": "graph"}
    if auth is not None:
        files[AUTH_FILENAME] = json.dumps(auth).encode("utf-8")
        file_metadata[AUTH_FILENAME] = {"kind": "auth"}
    if content is not None:
        files[CONTENT_FILENAME] = json.dumps(content).encode("utf-8")
        file_metadata[CONTENT_FILENAME] = {"kind": "content"}

    manifest = build_manifest(
        organization_id=org_id,
        source_store="surreal",
        files=files,
        file_metadata=file_metadata,
    )
    return LoadedArchive(source=Path(f"{org_id}.tar.gz"), manifest=manifest, files=files)


def _graph_payload(
    org_id: str,
    *,
    duplicate_entity_id: str,
    unique_entity_id: str,
    episode_id: str,
    mention_id: str,
) -> dict[str, object]:
    return {
        "version": "2.0",
        "created_at": "2026-04-26T00:00:00+00:00",
        "organization_id": org_id,
        "entity_count": 2,
        "relationship_count": 1,
        "episode_count": 1,
        "mention_count": 1,
        "entities": [
            {
                "id": duplicate_entity_id,
                "entity_type": "Person",
                "name": "Bliss",
                "organization_id": org_id,
                "metadata": {"source": org_id},
            },
            {
                "id": unique_entity_id,
                "entity_type": "Project",
                "name": unique_entity_id,
                "organization_id": org_id,
            },
        ],
        "relationships": [
            {
                "id": f"rel-{org_id}",
                "source_id": duplicate_entity_id,
                "relationship_type": "WORKS_ON",
                "target_id": unique_entity_id,
            }
        ],
        "episodes": [
            {
                "uuid": episode_id,
                "group_id": org_id,
                "content": f"episode from {org_id}",
                "entity_edges": [duplicate_entity_id, unique_entity_id],
            }
        ],
        "mentions": [
            {
                "uuid": mention_id,
                "group_id": org_id,
                "source_id": episode_id,
                "target_id": duplicate_entity_id,
            }
        ],
    }


def _auth_payload(org_id: str, *, role: str) -> dict[str, object]:
    return {
        "version": "1.0",
        "created_at": "2026-04-26T00:00:00+00:00",
        "tables": {
            "users": [{"uuid": "user-bliss", "email": "bliss@example.com"}],
            "organizations": [{"uuid": org_id, "name": org_id, "slug": org_id}],
            "organization_members": [
                {
                    "uuid": f"member-{org_id}",
                    "organization_id": org_id,
                    "user_id": "user-bliss",
                    "role": role,
                }
            ],
        },
        "row_counts": {"users": 1, "organizations": 1, "organization_members": 1},
        "total_rows": 3,
    }


def _content_payload(org_id: str, *, source_id: str, entity_id: str) -> dict[str, object]:
    return {
        "version": "1.0",
        "created_at": "2026-04-26T00:00:00+00:00",
        "tables": {
            "crawl_sources": [
                {
                    "uuid": source_id,
                    "organization_id": org_id,
                    "name": "Docs",
                    "url": "https://docs.example.test",
                }
            ],
            "raw_captures": [
                {
                    "uuid": f"capture-{source_id}",
                    "organization_id": org_id,
                    "entity_id": entity_id,
                    "entity_ids": [entity_id],
                }
            ],
        },
        "row_counts": {"crawl_sources": 1, "raw_captures": 1},
        "total_rows": 2,
    }


def test_merge_archives_collapses_entity_aliases_and_rewrites_graph_edges() -> None:
    result = merge_archives(
        [
            _archive(
                "org-a",
                graph=_graph_payload(
                    "org-a",
                    duplicate_entity_id="entity-bliss-a",
                    unique_entity_id="project-api",
                    episode_id="episode-a",
                    mention_id="mention-a",
                ),
            ),
            _archive(
                "org-b",
                graph=_graph_payload(
                    "org-b",
                    duplicate_entity_id="entity-bliss-b",
                    unique_entity_id="project-core",
                    episode_id="episode-b",
                    mention_id="mention-b",
                ),
            ),
        ],
        options=ArchiveMergeOptions(canonical_org_id=CANONICAL_ORG_ID),
    )

    assert validate_archive(result.archive) == []
    assert result.source_org_ids == ("org-a", "org-b")
    assert result.entity_alias_count == 1

    graph = graph_payload_from_archive(result.archive)
    assert graph is not None
    assert graph["organization_id"] == CANONICAL_ORG_ID
    assert graph["entity_count"] == 3
    assert {entity["id"] for entity in graph["entities"]} == {
        "entity-bliss-a",
        "project-api",
        "project-core",
    }
    assert graph["relationships"][1]["source_id"] == "entity-bliss-a"
    assert graph["episodes"][1]["group_id"] == CANONICAL_ORG_ID
    assert graph["episodes"][1]["entity_edges"][0] == "entity-bliss-a"
    assert graph["mentions"][1]["target_id"] == "entity-bliss-a"


def test_merge_archives_rewrites_auth_and_content_to_canonical_org() -> None:
    result = merge_archives(
        [
            _archive(
                "org-a",
                graph=_graph_payload(
                    "org-a",
                    duplicate_entity_id="entity-bliss-a",
                    unique_entity_id="project-api",
                    episode_id="episode-a",
                    mention_id="mention-a",
                ),
                auth=_auth_payload("org-a", role="member"),
                content=_content_payload(
                    "org-a",
                    source_id="source-a",
                    entity_id="entity-bliss-a",
                ),
            ),
            _archive(
                "org-b",
                graph=_graph_payload(
                    "org-b",
                    duplicate_entity_id="entity-bliss-b",
                    unique_entity_id="project-core",
                    episode_id="episode-b",
                    mention_id="mention-b",
                ),
                auth=_auth_payload("org-b", role="owner"),
                content=_content_payload(
                    "org-b",
                    source_id="source-b",
                    entity_id="entity-bliss-b",
                ),
            ),
        ],
        options=ArchiveMergeOptions(canonical_org_id=CANONICAL_ORG_ID),
    )

    assert validate_archive(result.archive) == []

    auth = json.loads(result.archive.files[AUTH_FILENAME].decode("utf-8"))
    assert auth["row_counts"]["organizations"] == 1
    assert auth["tables"]["organizations"][0]["uuid"] == CANONICAL_ORG_ID
    assert auth["tables"]["organizations"][0]["name"] == "org-a"
    assert auth["tables"]["organization_members"] == [
        {
            "uuid": "member-org-a",
            "organization_id": CANONICAL_ORG_ID,
            "user_id": "user-bliss",
            "role": "owner",
        }
    ]

    content = content_payload_from_archive(result.archive)
    assert content is not None
    assert content["row_counts"]["crawl_sources"] == 1
    assert content["row_counts"]["raw_captures"] == 2
    assert content["tables"]["crawl_sources"][0]["organization_id"] == CANONICAL_ORG_ID
    assert content["tables"]["raw_captures"][1]["entity_id"] == "entity-bliss-a"
    assert content["tables"]["raw_captures"][1]["entity_ids"] == ["entity-bliss-a"]


def test_merged_archive_can_be_written_and_reloaded(tmp_path: Path) -> None:
    result = merge_archives(
        [
            _archive(
                "org-a",
                graph=_graph_payload(
                    "org-a",
                    duplicate_entity_id="entity-bliss-a",
                    unique_entity_id="project-api",
                    episode_id="episode-a",
                    mention_id="mention-a",
                ),
            )
        ],
        options=ArchiveMergeOptions(canonical_org_id=CANONICAL_ORG_ID),
    )
    archive_path = tmp_path / "merged.tar.gz"

    write_archive(archive_path, manifest=result.archive.manifest, files=result.archive.files)
    loaded = load_archive(archive_path)

    assert validate_archive(loaded) == []
    assert loaded.manifest.organization_id == CANONICAL_ORG_ID


def test_merge_archives_can_override_canonical_org_name_and_slug() -> None:
    result = merge_archives(
        [_archive("org-a", auth=_auth_payload("org-a", role="owner"))],
        options=ArchiveMergeOptions(
            canonical_org_id=CANONICAL_ORG_ID,
            canonical_org_name="Merged Organization",
            canonical_org_slug="merged-organization",
        ),
    )

    auth = json.loads(result.archive.files[AUTH_FILENAME].decode("utf-8"))
    assert auth["tables"]["organizations"][0]["name"] == "Merged Organization"
    assert auth["tables"]["organizations"][0]["slug"] == "merged-organization"


def test_merge_archives_does_not_coalesce_users_by_email() -> None:
    result = merge_archives(
        [
            _archive(
                "org-a",
                auth={
                    "version": "1.0",
                    "created_at": "2026-04-26T00:00:00+00:00",
                    "tables": {
                        "users": [
                            {
                                "uuid": "user-a",
                                "email": "STEF@HYPERBLISS.TECH",
                                "name": "Stef A",
                                "is_admin": False,
                            }
                        ],
                        "organizations": [{"uuid": "org-a", "name": "Org A", "slug": "org-a"}],
                        "organization_members": [
                            {
                                "uuid": "member-a",
                                "organization_id": "org-a",
                                "user_id": "user-a",
                                "role": "member",
                            }
                        ],
                        "memory_spaces": [
                            {
                                "uuid": "space-a",
                                "organization_id": "org-a",
                                "memory_scope": "private",
                                "scope_key": "user-a",
                                "created_by_user_id": "user-a",
                            }
                        ],
                        "user_sessions": [
                            {"uuid": "session-a", "user_id": "user-a", "token_hash": "stale"}
                        ],
                    },
                    "row_counts": {},
                    "total_rows": 5,
                },
                content={
                    "version": "1.0",
                    "created_at": "2026-04-26T00:00:00+00:00",
                    "tables": {
                        "raw_captures": [
                            {
                                "uuid": "capture-a",
                                "organization_id": "org-a",
                                "principal_id": "user-a",
                                "memory_scope": "private",
                                "scope_key": "user-a",
                                "created_by_user_id": "user-a",
                            }
                        ]
                    },
                    "row_counts": {"raw_captures": 1},
                    "total_rows": 1,
                },
            ),
            _archive(
                "org-b",
                auth={
                    "version": "1.0",
                    "created_at": "2026-04-26T00:00:00+00:00",
                    "tables": {
                        "users": [
                            {
                                "uuid": "user-b",
                                "email": "stef@hyperbliss.tech",
                                "name": "Stef B",
                                "is_admin": True,
                            }
                        ],
                        "organizations": [{"uuid": "org-b", "name": "Org B", "slug": "org-b"}],
                        "organization_members": [
                            {
                                "uuid": "member-b",
                                "organization_id": "org-b",
                                "user_id": "user-b",
                                "role": "owner",
                            }
                        ],
                        "memory_spaces": [
                            {
                                "uuid": "space-b",
                                "organization_id": "org-b",
                                "memory_scope": "private",
                                "scope_key": "user-b",
                                "created_by_user_id": "user-b",
                            }
                        ],
                    },
                    "row_counts": {},
                    "total_rows": 4,
                },
                content={
                    "version": "1.0",
                    "created_at": "2026-04-26T00:00:00+00:00",
                    "tables": {
                        "source_imports": [
                            {
                                "uuid": "import-b",
                                "organization_id": "org-b",
                                "principal_id": "user-b",
                                "target_memory_scope": "private",
                                "target_scope_key": "user-b",
                            }
                        ]
                    },
                    "row_counts": {"source_imports": 1},
                    "total_rows": 1,
                },
            ),
        ],
        options=ArchiveMergeOptions(canonical_org_id=CANONICAL_ORG_ID),
    )

    assert result.user_alias_count == 0

    auth = json.loads(result.archive.files[AUTH_FILENAME].decode("utf-8"))
    assert auth["tables"]["users"] == [
        {"uuid": "user-a", "email": "stef@hyperbliss.tech", "name": "Stef A", "is_admin": False},
        {"uuid": "user-b", "email": "stef@hyperbliss.tech", "name": "Stef B", "is_admin": True},
    ]
    assert auth["tables"]["organization_members"] == [
        {
            "uuid": "member-a",
            "organization_id": CANONICAL_ORG_ID,
            "user_id": "user-a",
            "role": "member",
        },
        {
            "uuid": "member-b",
            "organization_id": CANONICAL_ORG_ID,
            "user_id": "user-b",
            "role": "owner",
        },
    ]
    assert auth["tables"]["memory_spaces"] == [
        {
            "uuid": "space-a",
            "organization_id": CANONICAL_ORG_ID,
            "memory_scope": "private",
            "scope_key": "user-a",
            "created_by_user_id": "user-a",
        },
        {
            "uuid": "space-b",
            "organization_id": CANONICAL_ORG_ID,
            "memory_scope": "private",
            "scope_key": "user-b",
            "created_by_user_id": "user-b",
        },
    ]
    assert "user_sessions" not in auth["tables"]

    content = json.loads(result.archive.files[CONTENT_FILENAME].decode("utf-8"))
    assert content["tables"]["raw_captures"][0]["principal_id"] == "user-a"
    assert content["tables"]["raw_captures"][0]["scope_key"] == "user-a"
    assert content["tables"]["raw_captures"][0]["created_by_user_id"] == "user-a"
    assert content["tables"]["source_imports"][0]["principal_id"] == "user-b"
    assert content["tables"]["source_imports"][0]["target_scope_key"] == "user-b"
