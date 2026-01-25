"""Admin endpoints for health, stats, backup, and restore."""

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
from sibyl.db.models import Organization, OrganizationRole

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
async def health() -> HealthResponse:
    """Get server health status."""
    try:
        from sibyl_core.tools.core import get_health

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
        from sibyl_core.tools.core import get_stats

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
    """Check if a Cypher query is read-only (no mutations)."""
    dangerous = ["CREATE", "SET", "DELETE", "REMOVE", "MERGE", "DROP", "DETACH"]
    upper = cypher.upper()
    return not any(d in upper for d in dangerous)


@router.post(
    "/debug/query",
    response_model=DebugQueryResponse,
    dependencies=[Depends(require_org_role(*_OWNER_ONLY))],
)
async def debug_query(
    request: DebugQueryRequest,
    org: Organization = Depends(get_current_organization),
) -> DebugQueryResponse:
    """Execute a read-only Cypher query for debugging.

    Allows direct graph inspection for development and troubleshooting.
    Only read-only queries are permitted (no CREATE, SET, DELETE, etc.).

    Requires organization OWNER role.
    """
    # Validate read-only
    if not _is_read_only(request.cypher):
        raise HTTPException(
            status_code=400,
            detail="Only read-only queries allowed (no CREATE, SET, DELETE, REMOVE, MERGE, DROP)",
        )

    try:
        from sibyl_core.graph.client import get_graph_client

        client = await get_graph_client()
        result = await client.execute_read_org(
            request.cypher,
            str(org.id),
            **request.params,
        )

        # Convert result records to dicts
        rows = []
        for record in result:
            # FalkorDB returns tuples/records - convert to dict
            if hasattr(record, "keys"):
                rows.append(dict(record))
            elif isinstance(record, (list, tuple)):
                rows.append({"value": record})
            else:
                rows.append({"value": record})

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
    - Active agent count

    Requires organization OWNER role.
    """
    from sibyl_core.logging import LogBuffer
    from sibyl_core.tools.core import get_health, get_stats

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
        stats = await get_stats(organization_id=str(org.id))
        entity_count = stats.get("total_entities", 0)
    except Exception:
        entity_count = 0

    # Check worker/queue health
    try:
        from sibyl.jobs.queue import get_pool

        pool = await get_pool()
        info = await pool.pool.info()
        queue_healthy = bool(info)
        # Count pending jobs
        queue_depth = info.get("pending_jobs", 0) if info else 0
        worker_healthy = bool(info.get("workers", 0)) if info else False
    except Exception:
        queue_healthy = False
        queue_depth = 0
        worker_healthy = False

    # Count active agents
    try:
        from sqlmodel import col, select

        from sibyl.db import get_session
        from sibyl.db.models import Agent, AgentStatus

        async with get_session() as session:
            result = await session.execute(
                select(Agent).where(
                    col(Agent.organization_id) == org.id,
                    col(Agent.status) == AgentStatus.RUNNING,
                )
            )
            active_agents = len(list(result.scalars().all()))
    except Exception:
        active_agents = 0

    # Get recent error logs
    buffer = LogBuffer.get()
    error_entries = buffer.tail(n=10, level="error")
    recent_errors = [e.to_dict() for e in error_entries]

    return DevStatusResponse(
        api_healthy=api_healthy,
        worker_healthy=worker_healthy,
        graph_healthy=graph_healthy,
        queue_healthy=queue_healthy,
        uptime_seconds=uptime,
        entity_count=entity_count,
        queue_depth=queue_depth,
        active_agents=active_agents,
        recent_errors=recent_errors,
    )


# === Startup Recovery ===


async def recover_stuck_sources() -> dict[str, Any]:
    """Recover sources stuck in IN_PROGRESS state after server restart.

    Should be called during server startup to clean up orphaned crawl jobs.

    Returns:
        Dict with counts of recovered sources
    """
    from sqlalchemy import func, select
    from sqlmodel import col

    from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk, get_session
    from sibyl.db.models import CrawlStatus

    recovered = 0
    completed = 0
    reset_to_pending = 0

    try:
        async with get_session() as session:
            # Find all sources stuck in IN_PROGRESS
            result = await session.execute(
                select(CrawlSource).where(col(CrawlSource.crawl_status) == CrawlStatus.IN_PROGRESS)
            )
            stuck_sources = list(result.scalars().all())

            if not stuck_sources:
                log.info("No stuck sources found during startup recovery")
                return {"recovered": 0, "completed": 0, "reset_to_pending": 0}

            log.warning(
                "Found stuck IN_PROGRESS sources",
                count=len(stuck_sources),
                sources=[s.name for s in stuck_sources],
            )

            for source in stuck_sources:
                # Count documents for this source
                doc_count_result = await session.execute(
                    select(func.count(CrawledDocument.id)).where(
                        col(CrawledDocument.source_id) == source.id
                    )
                )
                doc_count = doc_count_result.scalar() or 0

                # Count chunks
                chunk_count_result = await session.execute(
                    select(func.count(DocumentChunk.id))
                    .join(CrawledDocument)
                    .where(col(CrawledDocument.source_id) == source.id)
                )
                chunk_count = chunk_count_result.scalar() or 0

                old_status = source.crawl_status

                if doc_count > 0:
                    # Has documents - mark as completed
                    source.crawl_status = CrawlStatus.COMPLETED
                    source.document_count = doc_count
                    source.chunk_count = chunk_count
                    completed += 1
                else:
                    # No documents - reset to pending
                    source.crawl_status = CrawlStatus.PENDING
                    reset_to_pending += 1

                # Clear the stale job ID
                source.current_job_id = None

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

    except Exception as e:
        log.exception("Startup recovery failed", error=str(e))

    return {
        "recovered": recovered,
        "completed": completed,
        "reset_to_pending": reset_to_pending,
    }
