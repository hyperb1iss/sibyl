"""Admin endpoints for health, stats, and ingestion."""

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from sibyl.api.schemas import (
    BackfillRequest,
    BackfillResponse,
    BackupDataSchema,
    BackupResponse,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    RestoreRequest,
    RestoreResponse,
    StatsResponse,
)
from sibyl.api.websocket import broadcast_event
from sibyl.auth.dependencies import get_current_organization, require_org_role
from sibyl.db.models import Organization, OrganizationRole

log = structlog.get_logger()

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[
        Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN)),
    ],
)


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Get server health status."""
    try:
        from sibyl.tools.core import get_health

        health_data = await get_health()

        return HealthResponse(
            status=health_data.get("status", "unknown"),
            server_name=health_data.get("server_name", "sibyl"),
            uptime_seconds=health_data.get("uptime_seconds", 0),
            graph_connected=health_data.get("graph_connected", False),
            entity_counts=health_data.get("entity_counts", {}),
            errors=health_data.get("errors", []),
        )

    except Exception as e:
        log.exception("health_check_failed", error=str(e))
        return HealthResponse(
            status="unhealthy",
            server_name="sibyl",
            uptime_seconds=0,
            graph_connected=False,
            entity_counts={},
            errors=[str(e)],
        )


@router.get("/stats", response_model=StatsResponse)
async def stats(
    org: Organization = Depends(get_current_organization),
) -> StatsResponse:
    """Get knowledge graph statistics."""
    try:
        from sibyl.tools.core import get_stats

        stats_data = await get_stats(organization_id=str(org.id))

        return StatsResponse(
            entity_counts=stats_data.get("entity_counts", {}),
            total_entities=stats_data.get("total_entities", 0),
        )

    except Exception as e:
        log.exception("stats_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to retrieve stats. Please try again."
        ) from e


# Background ingestion state
_ingestion_status: dict[str, Any] = {
    "running": False,
    "progress": 0,
    "files_processed": 0,
    "entities_created": 0,
    "entities_updated": 0,
    "errors": [],
}


async def _run_ingestion(path: str | None, force: bool, group_id: str) -> None:
    """Run ingestion in background with progress updates."""
    global _ingestion_status  # noqa: PLW0603
    from sibyl.tools.admin import sync_wisdom_docs

    _ingestion_status = {
        "running": True,
        "progress": 0,
        "files_processed": 0,
        "entities_created": 0,
        "entities_updated": 0,
        "errors": [],
    }

    try:
        # Broadcast start
        await broadcast_event("ingest_progress", _ingestion_status)

        # Run ingestion
        result = await sync_wisdom_docs(path=path, force=force, group_id=group_id)

        # Update final status
        _ingestion_status = {
            "running": False,
            "progress": 100,
            "files_processed": result.files_processed,
            "entities_created": result.entities_created,
            "entities_updated": result.entities_updated,
            "errors": result.errors,
            "duration_seconds": result.duration_seconds,
            "success": result.success,
        }

        # Broadcast completion
        await broadcast_event("ingest_complete", _ingestion_status)

    except Exception as e:
        _ingestion_status = {
            "running": False,
            "progress": 0,
            "errors": [str(e)],
            "success": False,
        }
        await broadcast_event("ingest_complete", _ingestion_status)


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    org: Organization = Depends(get_current_organization),
) -> IngestResponse:
    """Trigger document ingestion.

    Runs in background and sends progress via WebSocket.
    """
    if _ingestion_status.get("running"):
        raise HTTPException(status_code=409, detail="Ingestion already in progress")

    # Start background ingestion
    background_tasks.add_task(_run_ingestion, request.path, request.force, str(org.id))

    # Wait a moment for it to start
    await asyncio.sleep(0.1)

    return IngestResponse(
        success=True,
        files_processed=0,
        entities_created=0,
        entities_updated=0,
        duration_seconds=0,
        errors=["Ingestion started in background"],
    )


@router.get("/ingest/status")
async def ingest_status() -> dict[str, Any]:
    """Get current ingestion status."""
    return _ingestion_status


# === Backup/Restore Endpoints ===


@router.post("/backup", response_model=BackupResponse)
async def create_backup(
    org: Organization = Depends(get_current_organization),
) -> BackupResponse:
    """Create a backup of all graph data for the organization.

    Returns JSON backup data that can be saved to a file or stored.
    """
    try:
        from sibyl.tools.admin import create_backup as do_backup

        result = await do_backup(organization_id=str(org.id))

        if not result.success or result.backup_data is None:
            raise HTTPException(status_code=500, detail=result.message)

        # Convert dataclass to schema
        backup_schema = BackupDataSchema(
            version=result.backup_data.version,
            created_at=result.backup_data.created_at,
            organization_id=result.backup_data.organization_id,
            entity_count=result.backup_data.entity_count,
            relationship_count=result.backup_data.relationship_count,
            entities=result.backup_data.entities,
            relationships=result.backup_data.relationships,
        )

        return BackupResponse(
            success=True,
            entity_count=result.entity_count,
            relationship_count=result.relationship_count,
            message=result.message,
            duration_seconds=result.duration_seconds,
            backup_data=backup_schema,
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("backup_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Backup failed. Please try again.") from e


@router.post("/restore", response_model=RestoreResponse)
async def restore_backup_endpoint(
    request: RestoreRequest,
    org: Organization = Depends(get_current_organization),
) -> RestoreResponse:
    """Restore graph data from a backup.

    Restores entities and relationships from backup JSON.
    By default, skips entities that already exist.
    """
    try:
        from sibyl.tools.admin import BackupData, restore_backup as do_restore

        # Convert schema to dataclass
        backup_data = BackupData(
            version=request.backup_data.version,
            created_at=request.backup_data.created_at,
            organization_id=request.backup_data.organization_id,
            entity_count=request.backup_data.entity_count,
            relationship_count=request.backup_data.relationship_count,
            entities=request.backup_data.entities,
            relationships=request.backup_data.relationships,
        )

        result = await do_restore(
            backup_data,
            organization_id=str(org.id),
            skip_existing=request.skip_existing,
        )

        return RestoreResponse(
            success=result.success,
            entities_restored=result.entities_restored,
            relationships_restored=result.relationships_restored,
            entities_skipped=result.entities_skipped,
            relationships_skipped=result.relationships_skipped,
            errors=result.errors,
            duration_seconds=result.duration_seconds,
        )

    except Exception as e:
        log.exception("restore_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Restore failed. Please try again.") from e


# === Backfill Endpoint ===


@router.post("/backfill/task-project-relationships", response_model=BackfillResponse)
async def backfill_task_relationships(
    request: BackfillRequest,
    org: Organization = Depends(get_current_organization),
) -> BackfillResponse:
    """Backfill missing BELONGS_TO relationships between tasks and projects.

    Finds tasks that have a project_id in metadata but no corresponding
    BELONGS_TO relationship edge, and creates the missing edges.

    Use dry_run=true to preview what would be created.
    """
    try:
        from sibyl.tools.admin import backfill_task_project_relationships

        result = await backfill_task_project_relationships(
            organization_id=str(org.id),
            dry_run=request.dry_run,
        )

        return BackfillResponse(
            success=result.success,
            relationships_created=result.relationships_created,
            tasks_without_project=result.tasks_without_project,
            tasks_already_linked=result.tasks_already_linked,
            errors=result.errors,
            duration_seconds=result.duration_seconds,
            dry_run=request.dry_run,
        )

    except Exception as e:
        log.exception("backfill_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Backfill failed. Please try again.") from e
