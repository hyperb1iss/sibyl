"""Manifest-driven archives for graph/runtime migration rehearsals."""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

ARCHIVE_VERSION = "1.0"
MANIFEST_FILENAME = "manifest.json"
LEGACY_METADATA_FILENAME = "metadata.json"
GRAPH_FILENAME = "graph.json"
POSTGRES_FILENAME = "postgres.sql"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ArchiveFileManifest:
    """One logical file within a migration archive."""

    path: str
    sha256: str
    size_bytes: int
    kind: str = "other"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArchiveFileManifest:
        return cls(
            path=str(payload["path"]),
            sha256=str(payload["sha256"]),
            size_bytes=int(payload["size_bytes"]),
            kind=str(payload.get("kind", "other")),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class ArchiveManifest:
    """Top-level archive manifest."""

    version: str
    created_at: str
    organization_id: str
    source_store: str
    files: dict[str, ArchiveFileManifest]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["files"] = {
            name: asdict(file_manifest) for name, file_manifest in self.files.items()
        }
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArchiveManifest:
        files = payload.get("files", {})
        return cls(
            version=str(payload.get("version") or ARCHIVE_VERSION),
            created_at=str(payload.get("created_at") or ""),
            organization_id=str(payload.get("organization_id") or ""),
            source_store=str(payload.get("source_store") or "unknown"),
            files={
                str(name): ArchiveFileManifest.from_dict(file_payload)
                for name, file_payload in files.items()
                if isinstance(file_payload, dict)
            },
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class LoadedArchive:
    """Archive contents loaded into memory for validation/import."""

    source: Path
    manifest: ArchiveManifest
    files: dict[str, bytes]


def build_manifest(
    *,
    organization_id: str,
    source_store: str,
    files: dict[str, bytes],
    file_metadata: dict[str, dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ArchiveManifest:
    file_metadata = file_metadata or {}
    return ArchiveManifest(
        version=ARCHIVE_VERSION,
        created_at=datetime.now(UTC).isoformat(),
        organization_id=organization_id,
        source_store=source_store,
        files={
            name: ArchiveFileManifest(
                path=name,
                sha256=_sha256_bytes(payload),
                size_bytes=len(payload),
                kind=file_metadata.get(name, {}).get("kind", "other"),
                metadata=dict(file_metadata.get(name, {})),
            )
            for name, payload in files.items()
        },
        metadata=dict(metadata or {}),
    )


def write_archive(
    output: Path,
    *,
    manifest: ArchiveManifest,
    files: dict[str, bytes],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sibyl_migrate_") as tmpdir:
        tmp_path = Path(tmpdir)
        manifest_path = tmp_path / MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        for name, payload in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        with tarfile.open(output, "w:gz", compresslevel=6) as tar:
            tar.add(manifest_path, arcname=MANIFEST_FILENAME)
            for name in sorted(files):
                tar.add(tmp_path / name, arcname=name)


def _load_archive_bytes(source: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}

    if source.is_dir():
        for path in source.rglob("*"):
            if path.is_file():
                files[path.relative_to(source).as_posix()] = path.read_bytes()
        return files

    if source.is_file() and (source.name.endswith(".tar.gz") or source.name.endswith(".tgz")):
        with tarfile.open(source, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                files[member.name] = extracted.read()
        return files

    msg = f"Unsupported archive source: {source}"
    raise ValueError(msg)


def _legacy_manifest_from_files(files: dict[str, bytes]) -> ArchiveManifest:
    metadata_bytes = files.get(LEGACY_METADATA_FILENAME)
    if metadata_bytes is None:
        msg = "Archive is missing manifest.json"
        raise ValueError(msg)

    metadata_payload = json.loads(metadata_bytes.decode("utf-8"))
    checksums = metadata_payload.get("files", {})
    manifest_files: dict[str, ArchiveFileManifest] = {}

    for name, payload in files.items():
        if name == LEGACY_METADATA_FILENAME:
            continue
        manifest_files[name] = ArchiveFileManifest(
            path=name,
            sha256=str(checksums.get(name) or _sha256_bytes(payload)),
            size_bytes=len(payload),
            kind="graph"
            if name == GRAPH_FILENAME
            else "postgres"
            if name == POSTGRES_FILENAME
            else "other",
        )

    return ArchiveManifest(
        version=str(metadata_payload.get("version") or "2.0"),
        created_at=str(metadata_payload.get("created_at") or ""),
        organization_id=str(metadata_payload.get("organization_id") or ""),
        source_store="legacy",
        files=manifest_files,
        metadata={k: v for k, v in metadata_payload.items() if k != "files"},
    )


def _select_legacy_payload(
    files: dict[str, bytes],
    *,
    canonical_name: str,
    patterns: tuple[str, ...],
    kind: str,
) -> tuple[str, bytes] | None:
    matches = [
        (name, payload)
        for name, payload in sorted(files.items())
        if name == canonical_name or any(fnmatch(name, pattern) for pattern in patterns)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        msg = (
            f"Archive contains multiple {kind} payload candidates: "
            + ", ".join(name for name, _ in matches)
        )
        raise ValueError(msg)
    return matches[0]


def _organization_id_from_graph_bytes(payload: bytes) -> str:
    try:
        graph_payload = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    organization_id = graph_payload.get("organization_id")
    return str(organization_id) if organization_id else ""


def _legacy_manifest_from_backup_payloads(
    files: dict[str, bytes],
) -> tuple[ArchiveManifest, dict[str, bytes]]:
    graph_match = _select_legacy_payload(
        files,
        canonical_name=GRAPH_FILENAME,
        patterns=("*_graph.json", "*graph_backup.json"),
        kind="graph",
    )
    postgres_match = _select_legacy_payload(
        files,
        canonical_name=POSTGRES_FILENAME,
        patterns=("*_pg.sql", "*pg_backup.sql"),
        kind="postgres",
    )

    if graph_match is None and postgres_match is None:
        msg = "Archive is missing manifest.json"
        raise ValueError(msg)

    normalized_files: dict[str, bytes] = {}
    file_metadata: dict[str, dict[str, Any]] = {}

    if graph_match is not None:
        original_path, payload = graph_match
        normalized_files[GRAPH_FILENAME] = payload
        file_metadata[GRAPH_FILENAME] = {
            "kind": "graph",
            "original_path": original_path,
        }

    if postgres_match is not None:
        original_path, payload = postgres_match
        normalized_files[POSTGRES_FILENAME] = payload
        file_metadata[POSTGRES_FILENAME] = {
            "kind": "postgres",
            "original_path": original_path,
        }

    manifest = build_manifest(
        organization_id=_organization_id_from_graph_bytes(normalized_files.get(GRAPH_FILENAME, b"")),
        source_store="legacy",
        files=normalized_files,
        file_metadata=file_metadata,
        metadata={"legacy_layout": "backup_payloads"},
    )
    return manifest, normalized_files


def load_archive(source: Path) -> LoadedArchive:
    files = _load_archive_bytes(source)
    manifest_bytes = files.pop(MANIFEST_FILENAME, None)
    if manifest_bytes is not None:
        manifest = ArchiveManifest.from_dict(json.loads(manifest_bytes.decode("utf-8")))
    else:
        metadata_bytes = files.get(LEGACY_METADATA_FILENAME)
        if metadata_bytes is not None:
            manifest = _legacy_manifest_from_files(files)
            files.pop(LEGACY_METADATA_FILENAME, None)
        else:
            manifest, files = _legacy_manifest_from_backup_payloads(files)
    return LoadedArchive(source=source, manifest=manifest, files=files)


def validate_archive(archive: LoadedArchive) -> list[str]:
    errors: list[str] = []

    if not archive.manifest.files:
        errors.append("manifest does not declare any files")

    for name, file_manifest in archive.manifest.files.items():
        payload = archive.files.get(name)
        if payload is None:
            errors.append(f"missing archive file: {name}")
            continue

        actual_sha = _sha256_bytes(payload)
        if actual_sha != file_manifest.sha256:
            errors.append(
                f"checksum mismatch for {name}: expected {file_manifest.sha256}, got {actual_sha}"
            )

        if len(payload) != file_manifest.size_bytes:
            errors.append(
                f"size mismatch for {name}: expected {file_manifest.size_bytes}, got {len(payload)}"
            )

    for unexpected in sorted(set(archive.files) - set(archive.manifest.files)):
        errors.append(f"unexpected archive file not listed in manifest: {unexpected}")

    graph_bytes = archive.files.get(GRAPH_FILENAME)
    if graph_bytes is not None:
        try:
            graph_payload = json.loads(graph_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"graph.json is not valid UTF-8 JSON: {exc}")
        else:
            declared_entities = graph_payload.get("entity_count")
            declared_relationships = graph_payload.get("relationship_count")
            declared_episodes = graph_payload.get("episode_count")
            declared_mentions = graph_payload.get("mention_count")
            entities = graph_payload.get("entities", [])
            relationships = graph_payload.get("relationships", [])
            episodes = graph_payload.get("episodes", [])
            mentions = graph_payload.get("mentions", [])

            if isinstance(declared_entities, int) and declared_entities != len(entities):
                errors.append(
                    "graph.json entity_count mismatch: "
                    f"declared {declared_entities}, found {len(entities)} entities"
                )
            if isinstance(declared_relationships, int) and declared_relationships != len(
                relationships
            ):
                errors.append(
                    "graph.json relationship_count mismatch: "
                    f"declared {declared_relationships}, found {len(relationships)} relationships"
                )
            if isinstance(declared_episodes, int) and declared_episodes != len(episodes):
                errors.append(
                    "graph.json episode_count mismatch: "
                    f"declared {declared_episodes}, found {len(episodes)} episodes"
                )
            if isinstance(declared_mentions, int) and declared_mentions != len(mentions):
                errors.append(
                    "graph.json mention_count mismatch: "
                    f"declared {declared_mentions}, found {len(mentions)} mentions"
                )

            payload_org_id = str(graph_payload.get("organization_id") or "")
            manifest_org_id = archive.manifest.organization_id
            if payload_org_id and manifest_org_id and payload_org_id != manifest_org_id:
                errors.append(
                    "graph.json organization_id mismatch: "
                    f"manifest {manifest_org_id}, payload {payload_org_id}"
                )

    return errors


def graph_payload_from_archive(archive: LoadedArchive) -> dict[str, Any] | None:
    payload = archive.files.get(GRAPH_FILENAME)
    if payload is None:
        return None
    return json.loads(payload.decode("utf-8"))


def normalize_relationship_payloads(
    relationships: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize legacy relationship rows to the effective restore shape.

    Restore semantics currently preserve the first unique
    ``(source_id, relationship_type, target_id)`` triplet, then let later
    duplicate UUIDs overwrite earlier rows because the active runtime uses a
    delete-by-uuid upsert. Verification has to model the same behavior or it
    will fail on legacy archives that contain duplicate edge rows.
    """

    deduped_by_triplet: list[dict[str, Any]] = []
    seen_triplets: set[tuple[str, str, str]] = set()

    for payload in relationships:
        source_id = str(payload.get("source_id") or payload.get("source_node_uuid") or "")
        relationship_type = str(
            payload.get("relationship_type") or payload.get("rel_type") or payload.get("name") or ""
        )
        target_id = str(payload.get("target_id") or payload.get("target_node_uuid") or "")
        triplet = (source_id, relationship_type, target_id)

        if all(triplet) and triplet in seen_triplets:
            continue
        if all(triplet):
            seen_triplets.add(triplet)
        deduped_by_triplet.append(payload)

    deduped_by_id: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for payload in deduped_by_triplet:
        relationship_id = str(payload.get("id") or payload.get("uuid") or "").strip()
        if not relationship_id:
            passthrough.append(payload)
            continue
        deduped_by_id[relationship_id] = payload

    return [*passthrough, *deduped_by_id.values()]


def normalize_mention_payloads(
    mentions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize legacy mention rows to the effective restore shape."""

    deduped_by_id: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []

    for payload in mentions:
        mention_id = str(payload.get("uuid") or "").strip()
        if not mention_id:
            passthrough.append(payload)
            continue
        deduped_by_id[mention_id] = payload

    return [*passthrough, *deduped_by_id.values()]


def effective_graph_counts(graph_payload: dict[str, Any]) -> dict[str, int]:
    """Return the effective graph counts after restore normalization."""

    entities = list(graph_payload.get("entities", []))
    relationships = list(graph_payload.get("relationships", []))
    episodes = list(graph_payload.get("episodes", []))
    mentions = list(graph_payload.get("mentions", []))
    return {
        "entity_count": int(graph_payload.get("entity_count") or len(entities)),
        "relationship_count": int(
            graph_payload.get("effective_relationship_count")
            or len(normalize_relationship_payloads(relationships))
        ),
        "episode_count": int(graph_payload.get("episode_count") or len(episodes)),
        "mention_count": int(
            graph_payload.get("effective_mention_count")
            or len(normalize_mention_payloads(mentions))
        ),
    }


__all__ = [
    "ARCHIVE_VERSION",
    "GRAPH_FILENAME",
    "LEGACY_METADATA_FILENAME",
    "MANIFEST_FILENAME",
    "POSTGRES_FILENAME",
    "ArchiveFileManifest",
    "ArchiveManifest",
    "LoadedArchive",
    "build_manifest",
    "effective_graph_counts",
    "graph_payload_from_archive",
    "load_archive",
    "normalize_mention_payloads",
    "normalize_relationship_payloads",
    "validate_archive",
    "write_archive",
]
