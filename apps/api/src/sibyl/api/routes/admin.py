"""Admin endpoints for health, stats, backup, restore, and audit."""

import csv
import io
import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlunparse
from uuid import UUID, uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.schemas import (
    AdminAuditEventResponse,
    AdminAuditListResponse,
    BackfillRequest,
    BackfillResponse,
    BackupDataSchema,
    BackupResponse,
    DebugQueryRequest,
    DebugQueryResponse,
    DevStatusResponse,
    HealthResponse,
    ProjectRecordBackfillItem,
    ProjectRecordBackfillRequest,
    ProjectRecordBackfillResponse,
    RestoreRequest,
    RestoreResponse,
    StatsResponse,
)
from sibyl.auth.dependencies import get_current_organization, get_current_user, require_org_role
from sibyl.config import settings
from sibyl.coordination import get_coordination_health
from sibyl.persistence.auth_runtime import (
    create_project_record,
    get_project_record_by_graph_id,
    list_audit_events,
    log_audit_event,
)
from sibyl.persistence.content_runtime import (
    get_content_read_session,
    get_source_sync_counts,
    list_crawl_sources,
    save_crawl_source_record,
)
from sibyl_core.audit import audit_event_resource
from sibyl_core.auth import AuthOrganization, AuthUser, OrganizationRole
from sibyl_core.models import CrawlStatus, Entity
from sibyl_core.models.entities import EntityType
from sibyl_core.utils import fingerprint_text
from sibyl_core.utils.query import query_tokens, upper_query_tokens

log = structlog.get_logger()

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    # No router-level auth - endpoints specify their own requirements
)

# Role sets for different permission levels
_READ_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER)
_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)


def _audit_details(row: dict[str, object]) -> dict[str, object]:
    details = row.get("details")
    if not isinstance(details, dict):
        return {}
    return {str(key): value for key, value in details.items()}


def _audit_event_response(row: dict[str, object]) -> AdminAuditEventResponse:
    return AdminAuditEventResponse(
        id=str(row.get("uuid") or ""),
        organization_id=str(row["organization_id"]) if row.get("organization_id") else None,
        user_id=str(row["user_id"]) if row.get("user_id") else None,
        action=str(row.get("action") or ""),
        resource=audit_event_resource(row),
        ip_address=str(row["ip_address"]) if row.get("ip_address") else None,
        user_agent=str(row["user_agent"]) if row.get("user_agent") else None,
        details=_audit_details(row),
        created_at=row.get("created_at") if isinstance(row.get("created_at"), datetime) else None,
    )


def _audit_filename(extension: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"sibyl-audit-{timestamp}.{extension}"


def _audit_csv_response(events: list[AdminAuditEventResponse]) -> Response:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "created_at",
            "action",
            "user_id",
            "organization_id",
            "resource",
            "ip_address",
            "user_agent",
            "details",
        ],
    )
    writer.writeheader()
    for event in events:
        writer.writerow(
            {
                "created_at": event.created_at.isoformat() if event.created_at else "",
                "action": event.action,
                "user_id": event.user_id or "",
                "organization_id": event.organization_id or "",
                "resource": event.resource or "",
                "ip_address": event.ip_address or "",
                "user_agent": event.user_agent or "",
                "details": json.dumps(event.details, sort_keys=True, default=str),
            }
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{_audit_filename("csv")}"'},
    )


async def get_graph_stats_payload(group_id: str) -> dict[str, object]:
    from sibyl.persistence.graph_runtime import get_graph_stats_payload as service

    return await service(group_id)


async def execute_debug_query(
    cypher: str,
    group_id: str,
    **params: object,
) -> list[dict[str, object]]:
    if _debug_query_uses_content_runtime(cypher):
        _validate_content_debug_query(cypher)
        from sibyl.persistence.content_runtime import execute_debug_query as service

        return await service(
            cypher,
            organization_id=group_id,
            group_id=group_id,
            org_id=group_id,
            **params,
        )

    from sibyl.persistence.graph_runtime import execute_debug_query as service

    return await service(cypher, group_id=group_id, **params)


def _surreal_http_base_url() -> str | None:
    from urllib.parse import urlparse

    resolved = settings.resolved_surreal_url
    parsed = urlparse(resolved)
    if parsed.scheme in {"ws", "wss", "http", "https"}:
        scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
        path = parsed.path.removesuffix("/rpc")
        return urlunparse((scheme, parsed.netloc, path, "", "", "")).rstrip("/")
    return None


def _parse_surreal_metric_names(body: str) -> list[str]:
    names: set[str] = set()
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        metric = line.split(maxsplit=1)[0].split("{", 1)[0]
        if metric:
            names.add(metric)
    return sorted(names)


def _surreal_metrics_sample(metric_names: list[str]) -> dict[str, bool]:
    interesting = (
        "surrealdb_statement_total",
        "surrealdb_transaction_conflicts_total",
        "surrealdb_statement_duration_seconds",
        "surrealdb_query_duration_seconds",
        "surrealdb_http_request_duration_seconds",
        "surrealdb_session_active",
        "surrealdb_live_query_active",
    )
    available = set(metric_names)
    return {
        name: name in available or any(metric.startswith(f"{name}_") for metric in available)
        for name in interesting
    }


async def get_surreal_observability_status() -> dict[str, object]:
    base_url = _surreal_http_base_url()
    status: dict[str, object] = {
        "configured": base_url is not None,
        "base_url": base_url,
        "health_http_status": None,
        "metrics_http_status": None,
        "metrics_available": False,
        "metric_count": 0,
        "metrics_sample": {},
        "error": None,
    }
    if base_url is None:
        return status

    auth: tuple[str, str] | None = None
    password = settings.surreal_password.get_secret_value()
    if settings.surreal_username and password:
        auth = (settings.surreal_username, password)

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            health_response = await client.get(f"{base_url}/health")
            status["health_http_status"] = health_response.status_code

            metrics_response = await client.get(f"{base_url}/metrics", auth=auth)
            status["metrics_http_status"] = metrics_response.status_code
            if metrics_response.status_code == 200:
                metric_names = _parse_surreal_metric_names(metrics_response.text)
                status["metrics_available"] = True
                status["metric_count"] = len(metric_names)
                status["metrics_sample"] = _surreal_metrics_sample(metric_names)
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
    return status


@router.get(
    "/health",
    response_model=HealthResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
async def health(
    org: AuthOrganization = Depends(get_current_organization),
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


@router.post(
    "/write-test",
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
async def write_test(
    org: AuthOrganization = Depends(get_current_organization),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    """Perform a short-lived graph write used by `sibyl doctor`."""
    probe_id = f"doctor_{uuid4().hex}"
    try:
        from sibyl.persistence.graph_runtime import execute_surreal_graph_query

        rows = await execute_surreal_graph_query(
            str(org.id),
            """
            CREATE sibyl_doctor_probe SET
                probe_id = $probe_id,
                group_id = $group_id,
                user_id = $user_id,
                created_at = time::now();
            DELETE sibyl_doctor_probe
                WHERE probe_id = $probe_id AND group_id = $org_group_id
                RETURN BEFORE;
            """,
            probe_id=probe_id,
            org_group_id=str(org.id),
            user_id=str(user.id),
        )
        if rows is None:
            raise RuntimeError("active graph runtime does not expose SurrealDB writes")
        return {"status": "ok", "probe_id": probe_id}
    except Exception as exc:
        log.exception("doctor_write_test_failed", org_id=str(org.id), error=str(exc))
        raise HTTPException(status_code=503, detail="Write probe failed") from exc


@router.get(
    "/stats",
    response_model=StatsResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
@handle_workflow_errors("stats")
async def stats(
    org: AuthOrganization = Depends(get_current_organization),
) -> StatsResponse:
    """Get knowledge graph statistics."""
    stats_data = await get_graph_stats_payload(str(org.id))

    return StatsResponse(
        entity_counts=stats_data.get("entity_counts", {}),
        total_entities=stats_data.get("total_entities", 0),
    )


@router.get(
    "/audit",
    response_model=AdminAuditListResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def list_admin_audit(
    org: AuthOrganization = Depends(get_current_organization),
    user_id: str | None = Query(default=None, description="Filter by actor user ID"),
    action: str | None = Query(default=None, description="Filter by audit action"),
    resource: str | None = Query(default=None, description="Filter by resource label or ID"),
    start_time: datetime | None = Query(default=None, description="Inclusive start time"),
    end_time: datetime | None = Query(default=None, description="Inclusive end time"),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum audit events"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
) -> AdminAuditListResponse:
    rows, total = await list_audit_events(
        organization_id=org.id,
        user_id=user_id,
        action=action,
        resource=resource,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )
    return AdminAuditListResponse(
        events=[_audit_event_response(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        has_more=total > offset + len(rows),
    )


@router.get(
    "/audit/export",
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def export_admin_audit(
    org: AuthOrganization = Depends(get_current_organization),
    export_format: Literal["csv", "json"] = Query(
        default="csv",
        alias="format",
        description="Export format",
    ),
    user_id: str | None = Query(default=None, description="Filter by actor user ID"),
    action: str | None = Query(default=None, description="Filter by audit action"),
    resource: str | None = Query(default=None, description="Filter by resource label or ID"),
    start_time: datetime | None = Query(default=None, description="Inclusive start time"),
    end_time: datetime | None = Query(default=None, description="Inclusive end time"),
    limit: int = Query(default=1000, ge=1, le=5000, description="Maximum exported events"),
) -> Response:
    rows, total = await list_audit_events(
        organization_id=org.id,
        user_id=user_id,
        action=action,
        resource=resource,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=0,
    )
    events = [_audit_event_response(row) for row in rows]
    payload = AdminAuditListResponse(
        events=events,
        total=total,
        limit=limit,
        offset=0,
        has_more=total > len(rows),
    )
    if export_format == "csv":
        return _audit_csv_response(events)
    return JSONResponse(
        jsonable_encoder(payload),
        headers={"Content-Disposition": f'attachment; filename="{_audit_filename("json")}"'},
    )


# === Backup/Restore Endpoints ===


@router.post(
    "/backup",
    response_model=BackupResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
@handle_workflow_errors("backup")
async def create_backup(
    org: AuthOrganization = Depends(get_current_organization),
) -> BackupResponse:
    """Create a backup of all graph data for the organization.

    Returns JSON backup data that can be saved to a file or stored.
    """
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


@router.post(
    "/restore",
    response_model=RestoreResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
@handle_workflow_errors("restore")
async def restore_backup_endpoint(
    request: RestoreRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> RestoreResponse:
    """Restore graph data from a backup.

    Restores entities and relationships from backup JSON.
    By default, skips entities that already exist.
    """
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


# === Backfill Endpoint ===


@router.post(
    "/backfill/task-project-relationships",
    response_model=BackfillResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
@handle_workflow_errors("backfill")
async def backfill_task_relationships(
    request: BackfillRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> BackfillResponse:
    """Backfill missing BELONGS_TO relationships between tasks and projects.

    Finds tasks that have a project_id in metadata but no corresponding
    BELONGS_TO relationship edge, and creates the missing edges.

    Use dry_run=true to preview what would be created.
    """
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


async def list_graph_projects_for_record_backfill(group_id: str) -> list[Entity]:
    from sibyl.persistence.graph_runtime import get_entity_graph_runtime

    runtime = await get_entity_graph_runtime(group_id)
    projects: list[Entity] = []
    offset = 0
    page_size = 1000
    while True:
        batch = await runtime.entity_manager.list_by_type(
            EntityType.PROJECT,
            limit=page_size,
            offset=offset,
            include_archived=True,
        )
        if not batch:
            return projects
        projects.extend(batch)
        offset += page_size


def _is_archived_project(entity: Entity) -> bool:
    return str((entity.metadata or {}).get("status") or "").lower() == "archived"


async def _project_record_exists(*, organization_id: UUID, graph_project_id: str) -> bool:
    try:
        await get_project_record_by_graph_id(
            organization_id=organization_id,
            graph_project_id=graph_project_id,
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            return False
        raise
    return True


@router.post(
    "/backfill/project-records",
    response_model=ProjectRecordBackfillResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def backfill_project_records(
    backfill_request: ProjectRecordBackfillRequest,
    request: Request,
    org: AuthOrganization = Depends(get_current_organization),
    user: AuthUser = Depends(get_current_user),
) -> ProjectRecordBackfillResponse:
    """Backfill missing auth project records from graph project entities."""
    started_at = perf_counter()
    items: list[ProjectRecordBackfillItem] = []
    errors: list[str] = []

    try:
        projects = await list_graph_projects_for_record_backfill(str(org.id))
        for project in projects:
            graph_project_id = str(project.id or "").strip()
            if not graph_project_id:
                items.append(
                    ProjectRecordBackfillItem(
                        graph_project_id="",
                        status="skipped",
                        reason="missing_graph_project_id",
                    )
                )
                continue
            if _is_archived_project(project):
                items.append(
                    ProjectRecordBackfillItem(
                        graph_project_id=graph_project_id,
                        status="skipped",
                        reason="archived_project",
                    )
                )
                continue

            try:
                if await _project_record_exists(
                    organization_id=org.id,
                    graph_project_id=graph_project_id,
                ):
                    items.append(
                        ProjectRecordBackfillItem(
                            graph_project_id=graph_project_id,
                            status="existing",
                        )
                    )
                    continue

                if backfill_request.dry_run:
                    items.append(
                        ProjectRecordBackfillItem(
                            graph_project_id=graph_project_id,
                            status="would_create",
                        )
                    )
                    continue

                await create_project_record(
                    organization_id=org.id,
                    owner_user_id=user.id,
                    graph_project_id=graph_project_id,
                    name=project.name,
                    description=project.description,
                )
                items.append(
                    ProjectRecordBackfillItem(
                        graph_project_id=graph_project_id,
                        status="created",
                    )
                )
            except Exception as exc:
                errors.append(f"{graph_project_id}: {type(exc).__name__}")
                items.append(
                    ProjectRecordBackfillItem(
                        graph_project_id=graph_project_id,
                        status="failed",
                        reason=type(exc).__name__,
                    )
                )

        created_ids = [
            item.graph_project_id
            for item in items
            if item.status == "created" and item.graph_project_id
        ]
        if created_ids:
            await log_audit_event(
                action="project_records.backfill",
                user_id=user.id,
                organization_id=org.id,
                request=request,
                details={
                    "created_project_ids": created_ids,
                    "created": len(created_ids),
                    "dry_run": backfill_request.dry_run,
                },
            )
    except Exception as exc:
        log.exception("project_record_backfill_failed", error=str(exc))
        raise HTTPException(
            status_code=500,
            detail="Project record backfill failed. Please try again.",
        ) from exc

    statuses = ("existing", "would_create", "created", "skipped", "failed")
    counts = {status: sum(1 for item in items if item.status == status) for status in statuses}
    return ProjectRecordBackfillResponse(
        success=not errors,
        dry_run=backfill_request.dry_run,
        existing=counts["existing"],
        would_create=counts["would_create"],
        created=counts["created"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        projects=items,
        errors=errors,
        duration_seconds=perf_counter() - started_at,
    )


# === Debug Query Endpoint ===

# OWNER role only for debug queries
_OWNER_ONLY = (OrganizationRole.OWNER,)
_CONTENT_DEBUG_TABLES = {
    "API_IDEMPOTENCY_RECORDS",
    "CRAWL_SOURCES",
    "CRAWLED_DOCUMENTS",
    "DOCUMENT_CHUNKS",
    "RAW_CAPTURES",
    "SOURCE_IMPORTS",
}


def _query_tokens(query: str) -> set[str]:
    return upper_query_tokens(query)


def _debug_query_uses_content_runtime(query: str) -> bool:
    return bool(_query_tokens(query) & _CONTENT_DEBUG_TABLES)


def _validate_content_debug_query(query: str) -> None:
    content_table_tokens = [
        token.upper() for token in query_tokens(query) if token.upper() in _CONTENT_DEBUG_TABLES
    ]
    if len(content_table_tokens) != 1:
        msg = "Content debug queries must inspect one content table at a time"
        raise ValueError(msg)


def _debug_params_for_org(params: dict[str, Any], *, group_id: str) -> dict[str, Any]:
    sanitized = dict(params)
    for key in ("group_id", "organization_id", "org_id"):
        supplied = sanitized.pop(key, None)
        if supplied is not None and str(supplied) != group_id:
            raise HTTPException(
                status_code=400,
                detail=f"{key} must match the current organization",
            )
    return sanitized


def _is_read_only(query: str) -> bool:
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
    return _query_tokens(query).isdisjoint(dangerous)


def _is_supported_debug_dialect(query: str) -> bool:
    return _query_tokens(query).isdisjoint({"CALL", "MATCH", "UNWIND"})


@router.post(
    "/debug/query",
    response_model=DebugQueryResponse,
    dependencies=[Depends(require_org_role(*_OWNER_ONLY))],
)
async def debug_query(
    request: DebugQueryRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> DebugQueryResponse:
    """Execute a read-only graph query for debugging.

    Allows direct graph inspection for development and troubleshooting.
    Only read-only queries are permitted.

    Requires organization OWNER role.
    """
    if not _is_read_only(request.cypher):
        raise HTTPException(
            status_code=400,
            detail="Only read-only queries allowed",
        )
    if not _is_supported_debug_dialect(request.cypher):
        raise HTTPException(
            status_code=400,
            detail="Surreal runtime debug queries must use read-only SurrealQL",
        )

    group_id = str(org.id)
    params = _debug_params_for_org(request.params, group_id=group_id)
    try:
        rows = await execute_debug_query(
            request.cypher,
            group_id=group_id,
            **params,
        )

        return DebugQueryResponse(
            rows=rows,
            row_count=len(rows),
        )

    except Exception as e:
        log.exception(
            "debug_query_failed",
            error_type=type(e).__name__,
            query_hash=fingerprint_text(request.cypher),
        )
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
    org: AuthOrganization = Depends(get_current_organization),
) -> DevStatusResponse:
    """Get comprehensive developer status dashboard.

    Aggregates health checks from all components:
    - API server health
    - Worker process status
    - Active graph runtime connectivity
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
    surreal_observability = await get_surreal_observability_status()

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
        surreal_observability=surreal_observability,
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
