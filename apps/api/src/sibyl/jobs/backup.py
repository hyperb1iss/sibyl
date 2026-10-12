"""Backup jobs for active auth, content, and graph runtimes.

Creates timestamped, compressed backup archives containing:
- SurrealDB auth and content snapshots when those runtimes are active
- Graph export
- Metadata JSON (checksums, counts, version info)

Archives are published once to the configured archive store (a shared
directory or an S3 prefix, see :mod:`sibyl.backup_archives`), so any API
replica can list, download and delete what any worker wrote.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import tarfile
import tempfile
import zlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from sibyl.api.event_types import WSEvent
from sibyl.backup_archives import backup_archive_store
from sibyl.backup_ids import generate_backup_id
from sibyl.config import settings
from sibyl.persistence.auth_archive import export_auth_archive_payload
from sibyl.persistence.backups_common import (
    BackupRecord,
    BackupRuntimeOptions,
    BackupStatus,
    resolve_backup_runtime_options,
    resolve_object_database_dump,
)
from sibyl.persistence.backups_runtime import (
    attach_backup_job,
    create_backup_record,
    delete_backup_record,
    find_backup_record,
    forget_backup_record,
    list_backups as list_backup_records,
    list_enabled_backup_settings,
    update_backup_record,
)
from sibyl.persistence.content_archive import export_content_archive_payload
from sibyl_core.backends.archive_store import (
    ArchiveConflict,
    ArchiveStore,
    Reservation,
    archive_name as archive_name_for,
    backup_id_of,
)

log = structlog.get_logger()

# Backup archive version for compatibility tracking
BACKUP_VERSION = "2.0"


def _backup_job_id(backup_id: str) -> str:
    return f"backup:{backup_id}"


@dataclass
class BackupMetadata:
    """Metadata stored in each backup archive."""

    version: str
    created_at: str
    organization_id: str
    hostname: str
    database_dump_tables: int
    graph_entities: int
    graph_relationships: int
    files: dict[str, str] = field(default_factory=dict)  # filename -> sha256
    lineage_validation: list[dict[str, str]] = field(default_factory=list)


@dataclass
class BackupResult:
    """Result of a backup operation."""

    success: bool
    backup_id: str
    archive_path: str | None
    archive_size_bytes: int
    database_dump_size_bytes: int
    graph_size_bytes: int
    entity_count: int
    relationship_count: int
    duration_seconds: float
    error: str | None = None


def _sha256_file(path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _write_json_file(path: Path, payload: object, *, default: Any | None = None) -> int:
    path.write_text(
        json.dumps(payload, indent=2, default=default),
        encoding="utf-8",
    )
    return path.stat().st_size


async def _write_json_file_async(
    path: Path,
    payload: object,
    *,
    default: Any | None = None,
) -> int:
    return await asyncio.to_thread(_write_json_file, path, payload, default=default)


async def _sha256_file_async(path: Path) -> str:
    return await asyncio.to_thread(_sha256_file, path)


def _create_backup_archive(
    archive_path: Path,
    entries: list[tuple[Path, str, bool]],
    *,
    compresslevel: int = 6,
) -> int:
    with tarfile.open(archive_path, "w:gz", compresslevel=compresslevel) as tar:
        for path, arcname, required in entries:
            if required or path.exists():
                tar.add(path, arcname=arcname)
    return archive_path.stat().st_size


async def _create_backup_archive_async(
    archive_path: Path,
    entries: list[tuple[Path, str, bool]],
    *,
    compresslevel: int = 6,
) -> int:
    return await asyncio.to_thread(
        _create_backup_archive,
        archive_path,
        entries,
        compresslevel=compresslevel,
    )


@dataclass(frozen=True)
class PublishedArchive:
    """What a published archive holds, as its record reports it."""

    size: int
    entity_count: int
    relationship_count: int
    graph_size: int


# Staging untouched this long belongs to a backup that is no longer running.
_STALE_STAGING_AGE = timedelta(days=1)
# Staged export JSON is typically five to ten times the size of its gzip
# archive, and the archive itself is written beside it before publishing.
_STAGING_EXPANSION = 11
_GZIP_CHUNK = 1024 * 1024


def _read_gzip_to_end(path: Path) -> None:
    """Decompress a whole gzip stream in chunks, so its CRC and length are checked.

    Raises EOFError for a truncated stream and BadGzipFile (an OSError) for a
    CRC, length or trailing-data mismatch.
    """
    with gzip.open(path, "rb") as stream:
        while stream.read(_GZIP_CHUNK):
            pass


def _verify_published_archive(
    store: ArchiveStore,
    name: str,
    organization_id: str,
    staging: Path | None,
) -> PublishedArchive | None:
    """Prove an archive already under this backup's name is this backup.

    The name is derived from the backup id, so only an earlier attempt of
    this same backup writes it. Before that archive is adopted it must match
    the digest the store recorded at publish time (S3 keeps one), decompress
    to the end of its gzip stream (the restore loader stops at the tar end
    marker and never reads the gzip CRC and length, so a truncated tail would
    otherwise pass), load as an API backup whose inventory, checksums and
    payloads pass the restore validator, and belong to this organization.
    Anything else raises ArchiveConflict and the archive is left untouched.
    None means there is no archive under the name.
    """
    from sibyl_core.migrate.archive import load_archive, validate_archive

    reader = store.open(name)
    if reader is None:
        return None
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=staging) as tmpdir:
        copy = Path(tmpdir) / name
        digest = hashlib.sha256()
        with copy.open("wb") as handle:
            for chunk in reader.chunks():
                digest.update(chunk)
                handle.write(chunk)
        # A damaged archive reads the same whichever store holds it, so the
        # gzip stream is checked before the digest the directory store lacks.
        try:
            _read_gzip_to_end(copy)
            loaded = load_archive(copy)
            problems = validate_archive(loaded)
        except (ValueError, OSError, EOFError, zlib.error, tarfile.TarError) as error:
            raise ArchiveConflict(f"{name} is not a readable backup archive") from error
        if reader.sha256 is not None and reader.sha256 != digest.hexdigest():
            raise ArchiveConflict(f"{name} differs from the digest recorded when it was published")
        metadata = loaded.manifest.metadata.get("backup_metadata")
        if (
            problems
            or not isinstance(metadata, dict)
            or loaded.manifest.organization_id != organization_id
        ):
            raise ArchiveConflict(f"{name} holds a different backup than this one")
        return PublishedArchive(
            size=copy.stat().st_size,
            entity_count=int(metadata["graph_entities"]),
            relationship_count=int(metadata["graph_relationships"]),
            graph_size=len(loaded.files.get("graph.json", b"")),
        )


async def _find_backup_record(backup_id: str) -> BackupRecord | None:
    try:
        return await find_backup_record(backup_id)
    except Exception as e:
        log.warning("backup_db_lookup_failed", backup_id=backup_id, error=str(e))
        return None


async def _estimated_backup_bytes(organization_id: str) -> int:
    """Room the next backup needs while it runs, from the last one's archive.

    The first backup of an organization has nothing to go on and only the
    store's reserve is checked; the staged JSON is checked again, at its real
    size, before the archive is written.
    """
    try:
        page = await list_backup_records(UUID(organization_id), limit=20, offset=0)
    except Exception as e:
        log.warning("backup_size_estimate_failed", organization_id=organization_id, error=str(e))
        return 0
    for record in page.backups:
        if record.status == BackupStatus.COMPLETED.value and record.size_bytes > 0:
            return record.size_bytes * _STAGING_EXPANSION
    return 0


async def _safe_broadcast(event: str, data: dict[str, Any], *, org_id: str | None) -> None:
    """Broadcast event via Redis pub/sub."""
    try:
        from sibyl.api.pubsub import publish_event

        await publish_event(event, data, org_id=org_id)
    except Exception:
        log.debug("Broadcast failed (Redis unavailable)", event=event)


async def _update_backup_db(
    backup_id: str,
    *,
    status: str | None = None,
    filename: str | None = None,
    file_path: str | None = None,
    size_bytes: int | None = None,
    entity_count: int | None = None,
    relationship_count: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    duration_seconds: float | None = None,
    error: str | None = None,
) -> None:
    """Update backup record in database."""
    try:
        backup = await update_backup_record(
            backup_id,
            status=status,
            filename=filename,
            file_path=file_path,
            size_bytes=size_bytes,
            entity_count=entity_count,
            relationship_count=relationship_count,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            error=error,
        )
        if backup is None:
            log.warning("backup_db_record_not_found", backup_id=backup_id)
            return
        log.debug("backup_db_updated", backup_id=backup_id, status=backup.status)

    except Exception as e:
        log.warning("backup_db_update_failed", backup_id=backup_id, error=str(e))


async def _build_and_publish(  # noqa: PLR0915
    store: ArchiveStore,
    staging: Path | None,
    archive_name: str,
    *,
    organization_id: str,
    backup_id: str,
    options: BackupRuntimeOptions,
    room: Reservation,
) -> PublishedArchive:
    """Export the organization, assemble its archive and publish it once."""
    import socket

    include_auth_snapshot = options.include_auth_snapshot
    include_content_snapshot = options.include_content_snapshot
    include_graph = options.include_graph

    # Assemble next to the store when it is a directory, so publishing is a
    # hard link on one filesystem; otherwise in the system temp dir.
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=staging) as tmpdir:
        tmp_path = Path(tmpdir)
        await asyncio.to_thread(room.track, tmp_path)
        auth_file = tmp_path / "auth.json"
        content_file = tmp_path / "content.json"
        graph_file = tmp_path / "graph.json"
        metadata_file = tmp_path / "metadata.json"

        auth_size = 0
        content_size = 0
        graph_size = 0
        entity_count = 0
        relationship_count = 0
        file_checksums: dict[str, str] = {}

        # Step 1: Surreal auth backup
        if include_auth_snapshot:
            log.info("backup_auth_snapshot_start", backup_id=backup_id)
            auth_payload = await export_auth_archive_payload(organization_id=organization_id)
            auth_size = await _write_json_file_async(auth_file, auth_payload, default=str)
            file_checksums["auth.json"] = await _sha256_file_async(auth_file)
            log.info("backup_auth_snapshot_complete", backup_id=backup_id, size_bytes=auth_size)

        # Step 2: Surreal content backup
        if include_content_snapshot:
            log.info("backup_content_snapshot_start", backup_id=backup_id)
            content_payload = await export_content_archive_payload(organization_id=organization_id)
            content_size = await _write_json_file_async(
                content_file,
                content_payload,
                default=str,
            )
            file_checksums["content.json"] = await _sha256_file_async(content_file)
            log.info(
                "backup_content_snapshot_complete",
                backup_id=backup_id,
                size_bytes=content_size,
            )

        # Step 3: Graph backup
        if include_graph:
            log.info("backup_graph_start", backup_id=backup_id, organization_id=organization_id)
            try:
                from dataclasses import asdict as dc_asdict

                from sibyl_core.tools.admin import create_backup

                graph_result = await create_backup(organization_id=organization_id)

                if not graph_result.success or graph_result.backup_data is None:
                    raise RuntimeError(f"Graph backup failed: {graph_result.message}")

                entity_count = graph_result.entity_count
                relationship_count = graph_result.relationship_count

                # Write graph backup
                backup_dict = dc_asdict(graph_result.backup_data)
                graph_size = await _write_json_file_async(
                    graph_file,
                    backup_dict,
                    default=str,
                )
                file_checksums["graph.json"] = await _sha256_file_async(graph_file)

                log.info(
                    "backup_graph_complete",
                    backup_id=backup_id,
                    entities=entity_count,
                    relationships=relationship_count,
                    size_bytes=graph_size,
                )

            except Exception as e:
                log.exception("backup_graph_failed", backup_id=backup_id, error=str(e))
                raise

        from sibyl_core.migrate.archive_lineage import seal_archive_lineage

        captured_graph = (
            await asyncio.to_thread(lambda: json.loads(graph_file.read_text()))
            if graph_file.exists()
            else None
        )
        captured_content = (
            await asyncio.to_thread(lambda: json.loads(content_file.read_text()))
            if content_file.exists()
            else None
        )
        sealed_graph, sealed_content, lineage_report = seal_archive_lineage(
            captured_graph, captured_content
        )
        if sealed_graph is not None:
            graph_size = await _write_json_file_async(graph_file, sealed_graph, default=str)
            file_checksums["graph.json"] = await _sha256_file_async(graph_file)
        if sealed_content is not None:
            content_size = await _write_json_file_async(content_file, sealed_content, default=str)
            file_checksums["content.json"] = await _sha256_file_async(content_file)

        # Step 4: Create metadata
        metadata = BackupMetadata(
            version=BACKUP_VERSION,
            created_at=datetime.now(UTC).isoformat(),
            organization_id=organization_id,
            hostname=socket.gethostname(),
            database_dump_tables=0,
            graph_entities=entity_count,
            graph_relationships=relationship_count,
            files=file_checksums,
            lineage_validation=lineage_report,
        )
        await _write_json_file_async(metadata_file, asdict(metadata))

        # Step 5: Create tar.gz archive. The staged JSON bounds its size, and
        # on a directory store it shares the volume with everything else.
        archive_tmp_path = tmp_path / archive_name
        await asyncio.to_thread(room.require, auth_size + content_size + graph_size)

        log.info("backup_archive_start", backup_id=backup_id)

        archive_entries = [(metadata_file, "metadata.json", True)]
        if include_auth_snapshot:
            archive_entries.append((auth_file, "auth.json", False))
        if include_content_snapshot:
            archive_entries.append((content_file, "content.json", False))
        if include_graph:
            archive_entries.append((graph_file, "graph.json", False))
        archive_size = await _create_backup_archive_async(
            archive_tmp_path,
            archive_entries,
            compresslevel=6,
        )
        # Published once, never replaced: a name that already holds these
        # bytes is a retried write that landed. Other bytes under the name
        # are an earlier attempt of this same backup that finished publishing
        # after it was given up on; adopt it once it proves to be ours.
        archive_sha256 = await _sha256_file_async(archive_tmp_path)
        try:
            await asyncio.to_thread(
                store.put_new, archive_name, archive_tmp_path, sha256=archive_sha256
            )
        except ArchiveConflict:
            earlier = await asyncio.to_thread(
                _verify_published_archive, store, archive_name, organization_id, staging
            )
            if earlier is None:
                raise
            log.info("backup_archive_adopted", backup_id=backup_id, reason="publish_conflict")
            return earlier
        return PublishedArchive(
            size=archive_size,
            entity_count=entity_count,
            relationship_count=relationship_count,
            graph_size=graph_size,
        )


async def run_backup(
    ctx: dict[str, Any],  # noqa: ARG001
    organization_id: str,
    *,
    include_database_dump: bool | None = None,
    include_graph: bool = True,
    backup_id: str | None = None,
) -> dict[str, Any]:
    """Create a complete backup archive.

    This job creates a timestamped .tar.gz archive containing:
    - auth.json: Surreal auth snapshot when auth runs on Surreal
    - content.json: Surreal content snapshot when content runs on Surreal
    - graph.json: graph runtime export
    - metadata.json: Archive metadata with checksums

    Args:
        ctx: arq context
        organization_id: Organization UUID to backup
        include_database_dump: Ignored by active backups; retained for API compatibility
        include_graph: Deprecated compatibility no-op; graph export is always included
        backup_id: Pre-generated backup ID (optional, for API-triggered backups)

    Returns:
        Dict with backup result details
    """
    import time

    start_time = time.time()
    started_at = datetime.now(UTC)
    backup_id = backup_id or generate_backup_id(organization_id)
    job_id = _backup_job_id(backup_id)
    options = resolve_backup_runtime_options(
        store=settings.store,
        auth_store=settings.auth_store,
        include_database_dump=include_database_dump,
        include_graph=include_graph,
    )
    include_database_dump = options.include_database_dump
    include_graph = options.include_graph
    include_auth_snapshot = options.include_auth_snapshot
    include_content_snapshot = options.include_content_snapshot

    # arq retries a job whose worker died or was cancelled, including after
    # the archive was published and recorded. A finished backup stays
    # finished: its result is reported again and nothing is rebuilt.
    record = await _find_backup_record(backup_id)
    if record is not None and str(record.organization_id) != organization_id:
        log.warning("backup_organization_mismatch", backup_id=backup_id)
        return {
            "success": False,
            "backup_id": backup_id,
            "job_id": job_id,
            "organization_id": organization_id,
            "error": "Backup belongs to another organization",
            "duration_seconds": 0.0,
        }
    if record is not None and record.status == BackupStatus.COMPLETED.value:
        log.info("backup_already_completed", backup_id=backup_id)
        return {
            "success": True,
            "backup_id": backup_id,
            "job_id": job_id,
            "organization_id": organization_id,
            "archive_path": record.file_path,
            "archive_size_bytes": record.size_bytes,
            "database_dump_size_bytes": 0,
            "graph_size_bytes": 0,
            "entity_count": record.entity_count,
            "relationship_count": record.relationship_count,
            "duration_seconds": record.duration_seconds,
            "already_completed": True,
        }

    # Update DB to mark as in progress
    await _update_backup_db(backup_id, status="in_progress", started_at=started_at)

    log.info(
        "backup_started",
        backup_id=backup_id,
        organization_id=organization_id,
        include_database_dump=include_database_dump,
        include_auth_snapshot=include_auth_snapshot,
        include_content_snapshot=include_content_snapshot,
        include_graph=include_graph,
    )

    await _safe_broadcast(
        WSEvent.BACKUP_STARTED,
        {"backup_id": backup_id, "organization_id": organization_id, "job_id": job_id},
        org_id=organization_id,
    )

    try:
        # A store that cannot publish fails the job before any export work.
        store = backup_archive_store()
        await asyncio.to_thread(store.ready)
        archive_name = archive_name_for(backup_id)
        archive_path = await asyncio.to_thread(store.location, archive_name)
        staging = await asyncio.to_thread(store.staging_dir)

        published = await asyncio.to_thread(
            _verify_published_archive, store, archive_name, organization_id, staging
        )
        if published is not None:
            # An earlier attempt published this backup and died before
            # recording it; its archive is complete, so keep it.
            log.info("backup_archive_adopted", backup_id=backup_id, reason="already_published")
        else:
            # Concurrent backups on one directory store count each other's
            # claims, so their exports together cannot overrun the volume.
            estimate = await _estimated_backup_bytes(organization_id)
            room = await asyncio.to_thread(store.reserve, backup_id, estimate)
            try:
                published = await _build_and_publish(
                    store,
                    staging,
                    archive_name,
                    organization_id=organization_id,
                    backup_id=backup_id,
                    options=options,
                    room=room,
                )
            finally:
                await asyncio.to_thread(room.release)
        archive_size = published.size
        entity_count = published.entity_count
        relationship_count = published.relationship_count
        graph_size = published.graph_size
        database_dump_size = 0
        duration = time.time() - start_time
        completed_at = datetime.now(UTC)

        log.info(
            "backup_complete",
            backup_id=backup_id,
            archive_path=archive_path,
            archive_size_bytes=archive_size,
            duration_seconds=duration,
        )

        # Update DB with completion status
        await _update_backup_db(
            backup_id,
            status="completed",
            filename=archive_name,
            file_path=archive_path,
            size_bytes=archive_size,
            entity_count=entity_count,
            relationship_count=relationship_count,
            completed_at=completed_at,
            duration_seconds=duration,
        )

        result_data = {
            "success": True,
            "backup_id": backup_id,
            "job_id": job_id,
            "organization_id": organization_id,
            "archive_path": archive_path,
            "archive_size_bytes": archive_size,
            "database_dump_size_bytes": database_dump_size,
            "graph_size_bytes": graph_size,
            "entity_count": entity_count,
            "relationship_count": relationship_count,
            "duration_seconds": duration,
        }

        await _safe_broadcast(WSEvent.BACKUP_COMPLETE, result_data, org_id=organization_id)

        return result_data

    except Exception as e:
        # Update DB with failure status
        duration = time.time() - start_time
        error_msg = str(e)

        log.exception("backup_failed", backup_id=backup_id, error=error_msg)

        await _update_backup_db(
            backup_id,
            status="failed",
            error=error_msg,
            duration_seconds=duration,
        )

        await _safe_broadcast(
            WSEvent.BACKUP_FAILED,
            {
                "backup_id": backup_id,
                "organization_id": organization_id,
                "job_id": job_id,
                "error": error_msg,
            },
            org_id=organization_id,
        )

        return {
            "success": False,
            "backup_id": backup_id,
            "job_id": job_id,
            "organization_id": organization_id,
            "error": error_msg,
            "duration_seconds": duration,
        }


async def _expired_org_backups(organization_id: UUID, cutoff: datetime) -> list[BackupRecord]:
    """Completed backups of one organization finished before the cutoff."""
    expired: list[BackupRecord] = []
    offset = 0
    page_size = 200
    while True:
        page = await list_backup_records(organization_id, limit=page_size, offset=offset)
        for record in page.backups:
            created = record.completed_at or record.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if record.status == BackupStatus.COMPLETED.value and created < cutoff:
                expired.append(record)
        offset += page_size
        if len(page.backups) < page_size:
            return expired


async def cleanup_old_backups(
    ctx: dict[str, Any],  # noqa: ARG001
    organization_id: str | None = None,
    *,
    retention_days: int | None = None,
) -> dict[str, Any]:
    """Remove backup archives older than the retention period, with their records.

    With an organization (the cleanup route), only that organization's
    completed backups are considered, found through their records, so one
    organization's admin never removes another's archives. Without one (the
    scheduled sweep), every archive in the store older than the operator's
    retention is removed, and the record of each is forgotten so no listing
    offers a download that no longer exists.

    Args:
        ctx: arq context
        organization_id: Limit the cleanup to this organization's backups
        retention_days: Override retention period (default: from settings)

    Returns:
        Dict with cleanup statistics
    """
    import time

    start_time = time.time()
    staging_removed = 0
    retention = retention_days or settings.backup_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=retention)
    store = backup_archive_store()
    deleted = 0
    freed_bytes = 0

    if organization_id is not None:
        org_uuid = UUID(organization_id)
        for record in await _expired_org_backups(org_uuid, cutoff):
            try:
                await asyncio.to_thread(store.delete, archive_name_for(record.backup_id))
                await delete_backup_record(org_uuid, record.backup_id)
            except Exception as e:
                log.warning("backup_delete_failed", backup_id=record.backup_id, error=str(e))
                continue
            deleted += 1
            freed_bytes += record.size_bytes
            log.info("backup_deleted", backup_id=record.backup_id, size_bytes=record.size_bytes)
    else:
        for archive in await asyncio.to_thread(store.archives):
            if archive.modified >= cutoff:
                continue
            backup_id = backup_id_of(archive.name)
            try:
                await asyncio.to_thread(store.delete, archive.name)
                await forget_backup_record(backup_id)
            except Exception as e:
                log.warning("backup_delete_failed", backup_id=backup_id, error=str(e))
                continue
            deleted += 1
            freed_bytes += archive.size
            log.info("backup_deleted", backup_id=backup_id, size_bytes=archive.size)
        # Staging left by a killed job holds plaintext export JSON on a
        # durable, shared volume; nothing a day old belongs to a live backup.
        try:
            stale = await asyncio.to_thread(
                store.sweep_stale, datetime.now(UTC) - _STALE_STAGING_AGE
            )
        except Exception as e:
            log.warning("backup_staging_sweep_failed", error=str(e))
        else:
            staging_removed = stale
            if stale:
                log.info("backup_staging_swept", removed=stale)

    duration = time.time() - start_time

    log.info(
        "backup_cleanup_complete",
        organization_id=organization_id,
        deleted=deleted,
        freed_bytes=freed_bytes,
        retention_days=retention,
        duration_seconds=duration,
    )

    return {
        "deleted": deleted,
        "freed_bytes": freed_bytes,
        "staging_removed": staging_removed,
        "retention_days": retention,
        "duration_seconds": duration,
    }


async def run_scheduled_backups(
    ctx: dict[str, Any],  # noqa: ARG001
) -> dict[str, Any]:
    """Run scheduled backups for all organizations with backups enabled.

    This job is triggered by the cron scheduler and queries all organizations
    that have backups enabled in their settings, then enqueues individual
    backup jobs for each.

    Args:
        ctx: arq context (contains redis pool)

    Returns:
        Dict with scheduling results
    """
    log.info("scheduled_backups_started")

    orgs_queued = 0
    orgs_skipped = 0
    errors = []

    try:
        enabled_settings = await list_enabled_backup_settings()
        log.info("scheduled_backups_found_orgs", count=len(enabled_settings))

        for org_settings in enabled_settings:
            org_id = str(org_settings.organization_id)
            backup_id = generate_backup_id(org_id)
            backup = None
            options = resolve_backup_runtime_options(
                store=settings.store,
                auth_store=settings.auth_store,
                include_database_dump=resolve_object_database_dump(org_settings),
                include_graph=org_settings.include_graph,
            )

            try:
                backup = await create_backup_record(
                    org_id=org_settings.organization_id,
                    backup_id=backup_id,
                    include_database_dump=options.include_database_dump,
                    include_graph=options.include_graph,
                    created_by_user_id=None,
                    triggered_by="scheduled",
                )

                from sibyl.jobs.queue import enqueue_backup

                job_id = await enqueue_backup(
                    org_id,
                    include_database_dump=options.include_database_dump,
                    include_graph=options.include_graph,
                    backup_id=backup_id,
                )

                await attach_backup_job(backup.id, job_id)

                log.info(
                    "scheduled_backup_queued",
                    organization_id=org_id,
                    backup_id=backup_id,
                    job_id=job_id,
                )
                orgs_queued += 1

            except Exception as e:
                if backup is not None:
                    try:
                        await delete_backup_record(org_settings.organization_id, backup_id)
                    except Exception as cleanup_error:
                        log.warning(
                            "scheduled_backup_cleanup_failed",
                            organization_id=org_id,
                            backup_id=backup_id,
                            error=str(cleanup_error),
                        )

                log.warning(
                    "scheduled_backup_failed_to_queue",
                    organization_id=org_id,
                    error=str(e),
                )
                errors.append({"organization_id": org_id, "error": str(e)})
                orgs_skipped += 1

    except Exception as e:
        log.exception("scheduled_backups_failed", error=str(e))
        return {
            "success": False,
            "orgs_queued": orgs_queued,
            "orgs_skipped": orgs_skipped,
            "errors": errors,
            "error": str(e),
        }

    log.info(
        "scheduled_backups_complete",
        orgs_queued=orgs_queued,
        orgs_skipped=orgs_skipped,
    )

    return {
        "success": True,
        "orgs_queued": orgs_queued,
        "orgs_skipped": orgs_skipped,
        "errors": errors,
    }
