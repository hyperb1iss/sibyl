"""Checked API backup envelopes adapted to the shared logical archive contract."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sibyl_core.migrate.archive import (
    ARCHIVE_VERSION,
    AUTH_FILENAME,
    BACKUP_METADATA_FILENAME,
    CONTENT_FILENAME,
    GRAPH_FILENAME,
    ArchiveFileManifest,
    ArchiveManifest,
    LoadedArchive,
    _sha256_bytes,
    validate_archive,
)


def _backup_json(data: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("API backup JSON contains duplicate keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("API backup JSON contains a nonfinite number")

    result = json.loads(data, object_pairs_hook=object_pairs, parse_constant=reject_constant)
    if not isinstance(result, dict):
        raise ValueError("API backup JSON must be an object")
    return result


def load_backup_archive(source: Path, files: dict[str, bytes]) -> LoadedArchive:
    """Adapt the API backup envelope without changing its logical payload bytes."""
    encoded = files.pop(BACKUP_METADATA_FILENAME, None)
    if encoded is None:
        raise ValueError("Archive is missing manifest.json or backup metadata.json")
    metadata = _backup_json(encoded)
    required = {
        "version",
        "created_at",
        "organization_id",
        "hostname",
        "database_dump_tables",
        "graph_entities",
        "graph_relationships",
        "files",
    }
    if (
        not isinstance(metadata, dict)
        or not required <= metadata.keys()
        or metadata.keys() - required - {"lineage_validation"}
        or metadata["version"] != "2.0"
    ):
        raise ValueError("Unsupported or malformed API backup metadata")
    organization = metadata["organization_id"]
    if not isinstance(organization, str) or str(UUID(organization)) != organization:
        raise ValueError("API backup requires a canonical organization UUID")
    if not isinstance(metadata["hostname"], str):
        raise ValueError("API backup hostname must be a string")
    created = metadata["created_at"]
    if not isinstance(created, str) or datetime.fromisoformat(created).tzinfo is None:
        raise ValueError("API backup requires a timezone-aware creation time")
    for count in ("database_dump_tables", "graph_entities", "graph_relationships"):
        if type(metadata[count]) is not int or metadata[count] < 0:
            raise ValueError("API backup counts must be nonnegative integers")
    if metadata["database_dump_tables"] != 0:
        raise ValueError("API backup database dumps are not supported by logical restore")
    checksums = metadata["files"]
    supported = {AUTH_FILENAME, CONTENT_FILENAME, GRAPH_FILENAME}
    if (
        not isinstance(checksums, dict)
        or not checksums
        or set(checksums) != set(files)
        or set(files) - supported
    ):
        raise ValueError("API backup file inventory differs from metadata")
    lineage = metadata.get("lineage_validation", [])
    if not isinstance(lineage, list) or any(
        not isinstance(row, dict) or any(not isinstance(v, str) for v in row.values())
        for row in lineage
    ):
        raise ValueError("API backup lineage report is malformed")
    entries = {}
    for name, data in files.items():
        checksum = checksums[name]
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(char not in "0123456789abcdef" for char in checksum)
            or _sha256_bytes(data) != checksum
        ):
            raise ValueError("API backup checksum mismatch")
        payload = _backup_json(data)
        versions = {
            AUTH_FILENAME: {"1.0"},
            CONTENT_FILENAME: {"1.0", "2.0", "2.1", "2.2", "2.3"},
            GRAPH_FILENAME: {"2.0", "3.0"},
        }
        if payload.get("version") not in versions[name]:
            raise ValueError("Unsupported API backup logical payload version")
        if not isinstance(payload, dict) or payload.get("organization_id") != organization:
            raise ValueError("API backup payload organization differs from metadata")
        if name == GRAPH_FILENAME:
            for count, field in (
                ("graph_entities", "entities"),
                ("graph_relationships", "relationships"),
            ):
                if (
                    not isinstance(payload.get(field), list)
                    or len(payload[field]) != metadata[count]
                ):
                    raise ValueError("API backup graph counts differ from metadata")
        else:
            tables = payload.get("tables")
            if not isinstance(tables, dict):
                raise ValueError("API backup tabular payload is malformed")
            for table, rows in tables.items():
                if not isinstance(rows, list):
                    raise ValueError("API backup table is malformed")
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("API backup row is malformed")
                    if "organization_id" in row and row["organization_id"] != organization:
                        raise ValueError("API backup row is outside its organization")
                    if (
                        name == AUTH_FILENAME
                        and table == "organizations"
                        and row.get("uuid") != organization
                    ):
                        raise ValueError("API backup organization row differs from metadata")
        entries[name] = ArchiveFileManifest(
            name, checksum, len(data), kind=name.removesuffix(".json")
        )
    if GRAPH_FILENAME not in files and (
        metadata["graph_entities"] or metadata["graph_relationships"]
    ):
        raise ValueError("API backup declares absent graph data")
    archive = LoadedArchive(
        source=source,
        manifest=ArchiveManifest(
            version=ARCHIVE_VERSION,
            created_at=created,
            organization_id=organization,
            source_store="surreal",
            files=entries,
            metadata={"backup_metadata": metadata},
        ),
        files=files,
    )
    if validate_archive(archive):
        raise ValueError("API backup logical payload validation failed")
    return archive
