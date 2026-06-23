"""Migration archive and verification helpers."""

from typing import Any

from sibyl_core.migrate.archive import (
    ARCHIVE_VERSION,
    AUTH_FILENAME,
    CONTENT_FILENAME,
    GRAPH_FILENAME,
    MANIFEST_FILENAME,
    ArchiveFileManifest,
    ArchiveManifest,
    LoadedArchive,
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
from sibyl_core.migrate.merge import (
    ArchiveMergeOptions,
    ArchiveMergeResult,
    EntityCollisionPolicy,
    UserCollisionPolicy,
    merge_archives,
)

_VERIFY_EXPORTS = {"GraphVerificationResult", "verify_graph_archive"}


def __getattr__(name: str) -> Any:
    if name not in _VERIFY_EXPORTS:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)

    from sibyl_core.migrate.verify import GraphVerificationResult, verify_graph_archive

    values = {
        "GraphVerificationResult": GraphVerificationResult,
        "verify_graph_archive": verify_graph_archive,
    }
    globals().update(values)
    return values[name]


__all__ = [
    "ARCHIVE_VERSION",
    "AUTH_FILENAME",
    "CONTENT_FILENAME",
    "GRAPH_FILENAME",
    "MANIFEST_FILENAME",
    "ArchiveFileManifest",
    "ArchiveManifest",
    "ArchiveMergeOptions",
    "ArchiveMergeResult",
    "EntityCollisionPolicy",
    "GraphVerificationResult",
    "LoadedArchive",
    "UserCollisionPolicy",
    "auth_payload_from_archive",
    "build_manifest",
    "content_payload_from_archive",
    "effective_graph_counts",
    "graph_payload_from_archive",
    "load_archive",
    "merge_archives",
    "normalize_mention_payloads",
    "normalize_relationship_payloads",
    "validate_archive",
    "verify_graph_archive",
    "write_archive",
]
