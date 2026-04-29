"""Admin endpoints for health, stats, backup, and restore."""

import re
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import (
    BackfillRequest,
    BackfillResponse,
    BackupDataSchema,
    BackupResponse,
    DebugQueryRequest,
    DebugQueryResponse,
    DevStatusResponse,
    HealthResponse,
    RestoreRequest,
    RestoreResponse,
    StatsResponse,
)
from sibyl.auth.dependencies import get_current_organization, require_org_role
from sibyl.coordination import get_coordination_health
from sibyl.db.models import CrawlStatus, Organization, OrganizationRole
from sibyl.persistence.content_runtime import (
    get_content_read_session,
    get_source_sync_counts,
    list_crawl_sources,
    save_crawl_source_record,
)
from sibyl.persistence.graph_runtime import (
    execute_debug_query,
    get_graph_stats_payload,
)

log = structlog.get_logger()

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    # No router-level auth - endpoints specify their own requirements
)

# Role sets for different permission levels
_READ_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER)
_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)


@router.get(
    "/health",
    response_model=HealthResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
async def health(
    org: Organization = Depends(get_current_organization),
) -> HealthResponse:
    """Get server health status."""
    try:
        from sibyl_core.tools.core import get_health

        health_data = await get_health(organization_id=str(org.id))

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


@router.get(
    "/stats",
    response_model=StatsResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
async def stats(
    org: Organization = Depends(get_current_organization),
) -> StatsResponse:
    """Get knowledge graph statistics."""
    try:
        stats_data = await get_graph_stats_payload(str(org.id))

        return StatsResponse(
            entity_counts=stats_data.get("entity_counts", {}),
            total_entities=stats_data.get("total_entities", 0),
        )

    except Exception as e:
        log.exception("stats_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to retrieve stats. Please try again."
        ) from e


# === Backup/Restore Endpoints ===


@router.post(
    "/backup",
    response_model=BackupResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def create_backup(
    org: Organization = Depends(get_current_organization),
) -> BackupResponse:
    """Create a backup of all graph data for the organization.

    Returns JSON backup data that can be saved to a file or stored.
    """
    try:
        from sibyl_core.tools.admin import create_backup as do_backup

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


@router.post(
    "/restore",
    response_model=RestoreResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def restore_backup_endpoint(
    request: RestoreRequest,
    org: Organization = Depends(get_current_organization),
) -> RestoreResponse:
    """Restore graph data from a backup.

    Restores entities and relationships from backup JSON.
    By default, skips entities that already exist.
    """
    try:
        from sibyl_core.tools.admin import BackupData, restore_backup as do_restore

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


@router.post(
    "/backfill/task-project-relationships",
    response_model=BackfillResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
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
        from sibyl_core.tools.admin import backfill_task_project_relationships

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


# === Debug Query Endpoint ===

# OWNER role only for debug queries
_OWNER_ONLY = (OrganizationRole.OWNER,)


def _is_read_only(cypher: str) -> bool:
    """Check if a graph query is read-only."""
    dangerous = [
        "ALTER",
        "CREATE",
        "DEFINE",
        "DELETE",
        "DROP",
        "INSERT",
        "MERGE",
        "REBUILD",
        "RELATE",
        "REMOVE",
        "SET",
        "UPSERT",
        "UPDATE",
    ]
    tokens = {token.upper() for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", cypher)}
    return not any(keyword in tokens for keyword in dangerous)


@router.post(
    "/debug/query",
    response_model=DebugQueryResponse,
    dependencies=[Depends(require_org_role(*_OWNER_ONLY))],
)
async def debug_query(
    request: DebugQueryRequest,
    org: Organization = Depends(get_current_organization),
) -> DebugQueryResponse:
    """Execute a read-only graph query for debugging.

    Allows direct graph inspection for development and troubleshooting.
    Only read-only queries are permitted.

    Requires organization OWNER role.
    """
    # Validate read-only
    if not _is_read_only(request.cypher):
        raise HTTPException(
            status_code=400,
            detail="Only read-only queries allowed",
        )

    try:
        rows = await execute_debug_query(
            request.cypher,
            group_id=str(org.id),
            **request.params,
        )

        return DebugQueryResponse(
            rows=rows,
            row_count=len(rows),
        )

    except Exception as e:
        log.exception("debug_query_failed", error=str(e), cypher=request.cypher[:100])
        return DebugQueryResponse(
            error=str(e),
            rows=[],
            row_count=0,
        )


# === Dev Status Dashboard ===


@router.get(
    "/dev-status",
    response_model=DevStatusResponse,
    dependencies=[Depends(require_org_role(*_OWNER_ONLY))],
)
async def dev_status(
    org: Organization = Depends(get_current_organization),
) -> DevStatusResponse:
    """Get comprehensive developer status dashboard.

    Aggregates health checks from all components:
    - API server health
    - Worker process status
    - FalkorDB connectivity
    - Job queue health
    - Recent error logs

    Requires organization OWNER role.
    """
    from sibyl_core.logging import LogBuffer
    from sibyl_core.tools.core import get_health

    # Get health and stats
    try:
        health = await get_health(organization_id=str(org.id))
        api_healthy = health.get("status") == "healthy"
        graph_healthy = health.get("graph_connected", False)
        uptime = health.get("uptime_seconds", 0)
    except Exception:
        api_healthy = True  # If we got here, API is up
        graph_healthy = False
        uptime = 0

    # Get entity count
    try:
        stats = await get_graph_stats_payload(str(org.id))
        entity_count = stats.get("total_entities", 0)
    except Exception:
        entity_count = 0

    coordination = await get_coordination_health()
    queue_healthy = coordination.get("queue_healthy", False)
    queue_depth = coordination.get("queue_depth", 0)
    worker_healthy = coordination.get("worker_healthy", False)

    # Get recent error logs
    buffer = LogBuffer.get()
    error_entries = buffer.tail(n=10, level="error")
    recent_errors = [e.to_dict() for e in error_entries]

    return DevStatusResponse(
        api_healthy=api_healthy,
        worker_healthy=worker_healthy,
        graph_healthy=graph_healthy,
        queue_healthy=queue_healthy,
        coordination_backend=coordination.get("backend", "unknown"),
        coordination_status=coordination.get("status", "unknown"),
        coordination_durable=coordination.get("durable", False),
        coordination_error=coordination.get("error"),
        uptime_seconds=uptime,
        entity_count=entity_count,
        queue_depth=queue_depth,
        recent_errors=recent_errors,
    )


# === Startup Recovery ===


async def recover_stuck_sources() -> dict[str, Any]:
    """Recover sources stuck in IN_PROGRESS state after server restart.

    Should be called during server startup to clean up orphaned crawl jobs.

    Returns:
        Dict with counts of recovered sources
    """
    recovered = 0
    completed = 0
    reset_to_pending = 0

    try:
        async with get_content_read_session() as session:
            stuck_sources = await list_crawl_sources(
                session,
                status=CrawlStatus.IN_PROGRESS,
                limit=None,
            )

            if not stuck_sources:
                log.info("No stuck sources found during startup recovery")
                return {"recovered": 0, "completed": 0, "reset_to_pending": 0}

            log.warning(
                "Found stuck IN_PROGRESS sources",
                count=len(stuck_sources),
                sources=[source.name for source in stuck_sources],
            )

            for source in stuck_sources:
                doc_count, chunk_count = await get_source_sync_counts(session, source_id=source.id)
                old_status = source.crawl_status

                if doc_count > 0:
                    source.crawl_status = CrawlStatus.COMPLETED
                    source.document_count = doc_count
                    source.chunk_count = chunk_count
                    completed += 1
                else:
                    source.crawl_status = CrawlStatus.PENDING
                    reset_to_pending += 1

                source.current_job_id = None
                await save_crawl_source_record(session, source=source)

                log.info(
                    "Recovered stuck source",
                    source_name=source.name,
                    old_status=old_status.value,
                    new_status=source.crawl_status.value,
                    doc_count=doc_count,
                )
                recovered += 1

        log.info(
            "Startup recovery complete",
            recovered=recovered,
            completed=completed,
            reset_to_pending=reset_to_pending,
        )
    except Exception as exc:
        log.exception("Startup recovery failed", error=str(exc))

    return {
        "recovered": recovered,
        "completed": completed,
        "reset_to_pending": reset_to_pending,
    }
