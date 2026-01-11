"""Backup jobs for PostgreSQL and FalkorDB graph data.

Creates timestamped, compressed backup archives containing:
- PostgreSQL dump (schema + data)
- FalkorDB graph export (entities + relationships)
- Metadata JSON (checksums, counts, version info)
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from sibyl.config import settings

log = structlog.get_logger()

# Backup archive version for compatibility tracking
BACKUP_VERSION = "2.0"


@dataclass
class BackupMetadata:
    """Metadata stored in each backup archive."""

    version: str
    created_at: str
    organization_id: str
    hostname: str
    pg_entities: int
    graph_entities: int
    graph_relationships: int
    files: dict[str, str] = field(default_factory=dict)  # filename -> sha256


@dataclass
class BackupResult:
    """Result of a backup operation."""

    success: bool
    backup_id: str
    archive_path: str | None
    archive_size_bytes: int
    pg_size_bytes: int
    graph_size_bytes: int
    entity_count: int
    relationship_count: int
    duration_seconds: float
    error: str | None = None


def _get_pg_env() -> dict[str, str]:
    """Get environment variables for pg_dump commands."""
    import os

    env = os.environ.copy()
    env["PGPASSWORD"] = settings.postgres_password.get_secret_value()
    return env


def _get_pg_connection_args() -> list[str]:
    """Get common pg_dump connection arguments."""
    return [
        "-h",
        settings.postgres_host,
        "-p",
        str(settings.postgres_port),
        "-U",
        settings.postgres_user,
        "-d",
        settings.postgres_db,
    ]


def _find_pg_tool(tool: str) -> str:
    """Find PostgreSQL tool (pg_dump) preferring newer versions."""
    # Homebrew keg paths to check (prefer newer versions)
    keg_paths = [
        f"/opt/homebrew/opt/postgresql@18/bin/{tool}",
        f"/opt/homebrew/opt/postgresql@17/bin/{tool}",
        f"/opt/homebrew/opt/postgresql@16/bin/{tool}",
        f"/usr/local/opt/postgresql@18/bin/{tool}",
        f"/usr/local/opt/postgresql@17/bin/{tool}",
        f"/usr/local/opt/postgresql@16/bin/{tool}",
    ]

    for path in keg_paths:
        if Path(path).exists():
            return path

    # Fall back to PATH lookup
    found = shutil.which(tool)
    if found:
        return found

    return tool  # Return bare name, will fail with FileNotFoundError


def _sha256_file(path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _generate_backup_id() -> str:
    """Generate a unique backup ID with timestamp."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"backup_{timestamp}"


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
        from sqlalchemy import select

        from sibyl.db.connection import get_session
        from sibyl.db.models import Backup, BackupSettings

        async with get_session() as session:
            # Get the backup record
            result = await session.execute(select(Backup).where(Backup.backup_id == backup_id))
            backup = result.scalar_one_or_none()

            if backup is None:
                log.warning("backup_db_record_not_found", backup_id=backup_id)
                return

            # Update fields (status is stored as string in DB)
            if status is not None:
                backup.status = status
            if filename is not None:
                backup.filename = filename
            if file_path is not None:
                backup.file_path = file_path
            if size_bytes is not None:
                backup.size_bytes = size_bytes
            if entity_count is not None:
                backup.entity_count = entity_count
            if relationship_count is not None:
                backup.relationship_count = relationship_count
            if started_at is not None:
                backup.started_at = started_at.replace(tzinfo=None)
            if completed_at is not None:
                backup.completed_at = completed_at.replace(tzinfo=None)
            if duration_seconds is not None:
                backup.duration_seconds = duration_seconds
            if error is not None:
                backup.error = error

            backup.updated_at = datetime.now(UTC).replace(tzinfo=None)

            # If completed, update org's backup settings
            if status == "completed":
                settings_result = await session.execute(
                    select(BackupSettings).where(
                        BackupSettings.organization_id == backup.organization_id
                    )
                )
                org_settings = settings_result.scalar_one_or_none()
                if org_settings:
                    org_settings.last_backup_at = (
                        completed_at.replace(tzinfo=None)
                        if completed_at
                        else datetime.now(UTC).replace(tzinfo=None)
                    )
                    org_settings.last_backup_id = backup_id

            await session.commit()
            log.debug("backup_db_updated", backup_id=backup_id, status=status)

    except Exception as e:
        log.warning("backup_db_update_failed", backup_id=backup_id, error=str(e))


async def run_backup(  # noqa: PLR0915
    ctx: dict[str, Any],  # noqa: ARG001
    organization_id: str,
    *,
    include_postgres: bool = True,
    include_graph: bool = True,
    backup_id: str | None = None,
) -> dict[str, Any]:
    """Create a complete backup archive.

    This job creates a timestamped .tar.gz archive containing:
    - postgres.sql: Full PostgreSQL dump
    - graph.json: FalkorDB graph export
    - metadata.json: Archive metadata with checksums

    Args:
        ctx: arq context
        organization_id: Organization UUID to backup
        include_postgres: Include PostgreSQL dump (default: True)
        include_graph: Include graph export (default: True)
        backup_id: Pre-generated backup ID (optional, for API-triggered backups)

    Returns:
        Dict with backup result details
    """
    import socket
    import time

    start_time = time.time()
    started_at = datetime.now(UTC)
    backup_id = backup_id or _generate_backup_id()

    # Update DB to mark as in progress
    await _update_backup_db(backup_id, status="in_progress", started_at=started_at)

    log.info(
        "backup_started",
        backup_id=backup_id,
        organization_id=organization_id,
        include_postgres=include_postgres,
        include_graph=include_graph,
    )

    await _safe_broadcast(
        "backup_started",
        {"backup_id": backup_id, "organization_id": organization_id},
        org_id=organization_id,
    )

    try:
        # Ensure backup directory exists
        backup_dir = settings.backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Work in a temp directory
        with tempfile.TemporaryDirectory(prefix="sibyl_backup_") as tmpdir:
            tmp_path = Path(tmpdir)
            pg_file = tmp_path / "postgres.sql"
            graph_file = tmp_path / "graph.json"
            metadata_file = tmp_path / "metadata.json"

            pg_size = 0
            graph_size = 0
            entity_count = 0
            relationship_count = 0
            file_checksums: dict[str, str] = {}

            # Step 1: PostgreSQL backup
            if include_postgres:
                log.info("backup_pg_start", backup_id=backup_id)
                try:
                    cmd = [
                        _find_pg_tool("pg_dump"),
                        *_get_pg_connection_args(),
                        "--format=plain",
                        "--no-owner",
                        "--no-acl",
                    ]

                    result = subprocess.run(  # noqa: S603, ASYNC221
                        cmd,
                        env=_get_pg_env(),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=600,  # 10 minute timeout
                    )

                    if result.returncode != 0:
                        raise RuntimeError(f"pg_dump failed: {result.stderr}")

                    pg_file.write_text(result.stdout, encoding="utf-8")
                    pg_size = pg_file.stat().st_size
                    file_checksums["postgres.sql"] = _sha256_file(pg_file)

                    log.info("backup_pg_complete", backup_id=backup_id, size_bytes=pg_size)

                except FileNotFoundError as e:
                    log.warning("backup_pg_not_found", error=str(e))
                    raise RuntimeError("pg_dump not found. Install PostgreSQL client tools.") from e
                except subprocess.TimeoutExpired as e:
                    log.warning("backup_pg_timeout", error=str(e))
                    raise RuntimeError("pg_dump timed out after 10 minutes") from e

            # Step 2: Graph backup
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
                    graph_file.write_text(
                        json.dumps(backup_dict, indent=2, default=str),
                        encoding="utf-8",
                    )
                    graph_size = graph_file.stat().st_size
                    file_checksums["graph.json"] = _sha256_file(graph_file)

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

            # Step 3: Create metadata
            metadata = BackupMetadata(
                version=BACKUP_VERSION,
                created_at=datetime.now(UTC).isoformat(),
                organization_id=organization_id,
                hostname=socket.gethostname(),
                pg_entities=0,  # Could count tables if needed
                graph_entities=entity_count,
                graph_relationships=relationship_count,
                files=file_checksums,
            )
            metadata_file.write_text(
                json.dumps(asdict(metadata), indent=2),
                encoding="utf-8",
            )

            # Step 4: Create tar.gz archive
            archive_name = f"sibyl_{backup_id}.tar.gz"
            archive_path = backup_dir / archive_name

            log.info("backup_archive_start", backup_id=backup_id, archive_path=str(archive_path))

            with tarfile.open(archive_path, "w:gz", compresslevel=6) as tar:
                tar.add(metadata_file, arcname="metadata.json")
                if include_postgres and pg_file.exists():
                    tar.add(pg_file, arcname="postgres.sql")
                if include_graph and graph_file.exists():
                    tar.add(graph_file, arcname="graph.json")

            archive_size = archive_path.stat().st_size
            duration = time.time() - start_time
            completed_at = datetime.now(UTC)

            log.info(
                "backup_complete",
                backup_id=backup_id,
                archive_path=str(archive_path),
                archive_size_bytes=archive_size,
                duration_seconds=duration,
            )

            # Update DB with completion status
            await _update_backup_db(
                backup_id,
                status="completed",
                filename=archive_name,
                file_path=str(archive_path),
                size_bytes=archive_size,
                entity_count=entity_count,
                relationship_count=relationship_count,
                completed_at=completed_at,
                duration_seconds=duration,
            )

            result_data = {
                "success": True,
                "backup_id": backup_id,
                "archive_path": str(archive_path),
                "archive_size_bytes": archive_size,
                "pg_size_bytes": pg_size,
                "graph_size_bytes": graph_size,
                "entity_count": entity_count,
                "relationship_count": relationship_count,
                "duration_seconds": duration,
            }

            await _safe_broadcast("backup_complete", result_data, org_id=organization_id)

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
            "backup_failed",
            {"backup_id": backup_id, "error": error_msg},
            org_id=organization_id,
        )

        return {
            "success": False,
            "backup_id": backup_id,
            "error": error_msg,
            "duration_seconds": duration,
        }


async def cleanup_old_backups(
    ctx: dict[str, Any],  # noqa: ARG001
    *,
    retention_days: int | None = None,
) -> dict[str, Any]:
    """Clean up backup archives older than retention period.

    Args:
        ctx: arq context
        retention_days: Override retention period (default: from settings)

    Returns:
        Dict with cleanup statistics
    """
    import time

    start_time = time.time()
    retention = retention_days or settings.backup_retention_days
    cutoff = datetime.now(UTC).timestamp() - (retention * 86400)

    backup_dir = settings.backup_dir
    if not backup_dir.exists():
        return {"deleted": 0, "freed_bytes": 0, "duration_seconds": 0}

    deleted = 0
    freed_bytes = 0

    for archive in backup_dir.glob("sibyl_backup_*.tar.gz"):
        try:
            if archive.stat().st_mtime < cutoff:
                size = archive.stat().st_size
                archive.unlink()
                deleted += 1
                freed_bytes += size
                log.info("backup_deleted", path=str(archive), size_bytes=size)
        except Exception as e:
            log.warning("backup_delete_failed", path=str(archive), error=str(e))

    duration = time.time() - start_time

    log.info(
        "backup_cleanup_complete",
        deleted=deleted,
        freed_bytes=freed_bytes,
        retention_days=retention,
        duration_seconds=duration,
    )

    return {
        "deleted": deleted,
        "freed_bytes": freed_bytes,
        "retention_days": retention,
        "duration_seconds": duration,
    }


def list_backups() -> list[dict[str, Any]]:
    """List available backup archives.

    Returns:
        List of backup info dicts sorted by creation time (newest first)
    """
    backup_dir = settings.backup_dir
    if not backup_dir.exists():
        return []

    backups = []

    for archive in backup_dir.glob("sibyl_backup_*.tar.gz"):
        try:
            stat = archive.stat()

            # Try to extract metadata for details (optional, may fail for corrupted archives)
            metadata = None
            try:
                with tarfile.open(archive, "r:gz") as tar:
                    member = tar.getmember("metadata.json")
                    f = tar.extractfile(member)
                    if f:
                        metadata = json.load(f)
            except Exception:  # noqa: S110
                pass  # Metadata extraction is optional

            # Parse backup_id from filename
            backup_id = archive.stem.replace("sibyl_", "").replace(".tar", "")

            backups.append(
                {
                    "backup_id": backup_id,
                    "filename": archive.name,
                    "path": str(archive),
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                    "metadata": metadata,
                }
            )
        except Exception as e:
            log.warning("backup_list_error", path=str(archive), error=str(e))

    # Sort by creation time (newest first)
    backups.sort(key=lambda b: b["created_at"], reverse=True)
    return backups


def get_backup(backup_id: str) -> dict[str, Any] | None:
    """Get details of a specific backup.

    Args:
        backup_id: The backup ID (e.g., 'backup_20260110_153045')

    Returns:
        Backup info dict or None if not found
    """
    backup_dir = settings.backup_dir
    archive_path = backup_dir / f"sibyl_{backup_id}.tar.gz"

    if not archive_path.exists():
        return None

    try:
        stat = archive_path.stat()

        # Extract full metadata (optional, may fail for corrupted archives)
        metadata = None
        files_in_archive = []
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                files_in_archive = tar.getnames()
                member = tar.getmember("metadata.json")
                f = tar.extractfile(member)
                if f:
                    metadata = json.load(f)
        except Exception:  # noqa: S110
            pass  # Metadata extraction is optional

        return {
            "backup_id": backup_id,
            "filename": archive_path.name,
            "path": str(archive_path),
            "size_bytes": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "metadata": metadata,
            "files": files_in_archive,
        }
    except Exception as e:
        log.warning("backup_get_error", backup_id=backup_id, error=str(e))
        return None


def delete_backup(backup_id: str) -> bool:
    """Delete a specific backup archive.

    Args:
        backup_id: The backup ID to delete

    Returns:
        True if deleted, False if not found
    """
    backup_dir = settings.backup_dir
    archive_path = backup_dir / f"sibyl_{backup_id}.tar.gz"

    if not archive_path.exists():
        return False

    try:
        size = archive_path.stat().st_size
        archive_path.unlink()
        log.info("backup_deleted", backup_id=backup_id, size_bytes=size)
        return True
    except Exception as e:
        log.warning("backup_delete_failed", backup_id=backup_id, error=str(e))
        return False


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
    from sqlalchemy import select

    from sibyl.db.connection import get_session
    from sibyl.db.models import Backup, BackupSettings, BackupStatus

    log.info("scheduled_backups_started")

    orgs_queued = 0
    orgs_skipped = 0
    errors = []

    try:
        async with get_session() as session:
            # Get all orgs with backups enabled
            result = await session.execute(
                select(BackupSettings).where(BackupSettings.enabled == True)  # noqa: E712
            )
            enabled_settings = result.scalars().all()

            log.info("scheduled_backups_found_orgs", count=len(enabled_settings))

            for org_settings in enabled_settings:
                org_id = str(org_settings.organization_id)

                try:
                    # Generate backup ID
                    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
                    backup_id = f"backup_{timestamp}"

                    # Create backup record
                    backup = Backup(
                        organization_id=org_settings.organization_id,
                        backup_id=backup_id,
                        status=BackupStatus.PENDING.value,
                        include_postgres=org_settings.include_postgres,
                        include_graph=org_settings.include_graph,
                        triggered_by="scheduled",
                    )
                    session.add(backup)
                    await session.flush()

                    # Enqueue the backup job
                    from sibyl.jobs.queue import enqueue_backup

                    job_id = await enqueue_backup(
                        org_id,
                        include_postgres=org_settings.include_postgres,
                        include_graph=org_settings.include_graph,
                        backup_id=backup_id,
                    )

                    # Update backup with job ID
                    backup.job_id = job_id
                    await session.commit()

                    log.info(
                        "scheduled_backup_queued",
                        organization_id=org_id,
                        backup_id=backup_id,
                        job_id=job_id,
                    )
                    orgs_queued += 1

                except Exception as e:
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
