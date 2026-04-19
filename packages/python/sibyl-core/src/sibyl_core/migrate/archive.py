"""Manifest-driven archives for graph/runtime migration rehearsals."""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
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

    if source.is_file() and (
        source.name.endswith(".tar.gz") or source.name.endswith(".tgz")
    ):
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
            kind="graph" if name == GRAPH_FILENAME else "postgres" if name == POSTGRES_FILENAME else "other",
        )

    return ArchiveManifest(
        version=str(metadata_payload.get("version") or "2.0"),
        created_at=str(metadata_payload.get("created_at") or ""),
        organization_id=str(metadata_payload.get("organization_id") or ""),
        source_store="legacy",
        files=manifest_files,
        metadata={k: v for k, v in metadata_payload.items() if k != "files"},
    )


def load_archive(source: Path) -> LoadedArchive:
    files = _load_archive_bytes(source)
    manifest_bytes = files.pop(MANIFEST_FILENAME, None)
    if manifest_bytes is not None:
        manifest = ArchiveManifest.from_dict(json.loads(manifest_bytes.decode("utf-8")))
    else:
        manifest = _legacy_manifest_from_files(files)
        files.pop(LEGACY_METADATA_FILENAME, None)
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

    return errors


def graph_payload_from_archive(archive: LoadedArchive) -> dict[str, Any] | None:
    payload = archive.files.get(GRAPH_FILENAME)
    if payload is None:
        return None
    return json.loads(payload.decode("utf-8"))


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
    "graph_payload_from_archive",
    "load_archive",
    "validate_archive",
    "write_archive",
]
