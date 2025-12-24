"""Admin endpoints for health, stats, and ingestion."""

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from sibyl.api.schemas import HealthResponse, IngestRequest, IngestResponse, StatsResponse
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
        raise HTTPException(status_code=500, detail=str(e)) from e


# Background ingestion state
_ingestion_status: dict[str, Any] = {
    "running": False,
    "progress": 0,
    "files_processed": 0,
    "entities_created": 0,
    "entities_updated": 0,
    "errors": [],
}


async def _run_ingestion(path: str | None, force: bool) -> None:
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
        result = await sync_wisdom_docs(path=path, force=force)

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
async def ingest(request: IngestRequest, background_tasks: BackgroundTasks) -> IngestResponse:
    """Trigger document ingestion.

    Runs in background and sends progress via WebSocket.
    """
    if _ingestion_status.get("running"):
        raise HTTPException(status_code=409, detail="Ingestion already in progress")

    # Start background ingestion
    background_tasks.add_task(_run_ingestion, request.path, request.force)

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
