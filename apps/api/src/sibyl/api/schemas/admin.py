"""Admin, telemetry, backup/restore, backfill, and debug request/response models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AdminAuditEventResponse(BaseModel):
    """Organization audit event for admin review."""

    id: str = Field(..., description="Audit event UUID")
    organization_id: str | None = Field(default=None, description="Organization ID")
    user_id: str | None = Field(default=None, description="Actor user ID")
    action: str = Field(..., description="Audit action")
    resource: str | None = Field(default=None, description="Best-effort resource label")
    ip_address: str | None = Field(default=None, description="Client IP address")
    user_agent: str | None = Field(default=None, description="Client user agent")
    details: dict[str, Any] = Field(default_factory=dict, description="Bounded event details")
    created_at: datetime | None = Field(default=None, description="Audit event timestamp")


class AdminAuditListResponse(BaseModel):
    """Paginated organization audit event response."""

    events: list[AdminAuditEventResponse]
    total: int
    limit: int
    offset: int
    has_more: bool


class HealthResponse(BaseModel):
    """Server health status."""

    status: Literal["healthy", "unhealthy", "unknown"]
    server_name: str
    uptime_seconds: int
    graph_connected: bool
    entity_counts: dict[str, int]
    errors: list[str]


class StatsResponse(BaseModel):
    """Knowledge graph statistics."""

    entity_counts: dict[str, int]
    total_entities: int
    relationship_counts: dict[str, int] | None = None
    total_relationships: int | None = None


class TelemetryDurationSummary(BaseModel):
    """Latency and error summary for a runtime surface."""

    count: int = 0
    errors: int = 0
    slow: int = 0
    error_rate: float = 0.0
    avg_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0


class TelemetryTrendPoint(BaseModel):
    """Minute-bucketed runtime trend point for overview charts."""

    timestamp: str
    api_p95_ms: float = 0.0
    surreal_p95_ms: float = 0.0
    memory_p95_ms: float = 0.0
    llm_p95_ms: float = 0.0
    error_rate: float = 0.0
    request_count: int = 0
    query_count: int = 0
    memory_count: int = 0
    llm_count: int = 0


class TelemetryEventResponse(BaseModel):
    """Recent bounded runtime event."""

    timestamp: str
    category: str
    status: str
    duration_ms: float | None = None
    value: float = 1.0
    labels: dict[str, str] = Field(default_factory=dict)


class TelemetryMetricResponse(BaseModel):
    """Counter, gauge, or histogram snapshot."""

    kind: str
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    value: float | None = None
    count: int | None = None
    sum: float | None = None
    min: float | None = None
    max: float | None = None
    avg: float | None = None
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None


class TelemetrySummaryResponse(BaseModel):
    """Runtime telemetry summary for the application overview."""

    generated_at: str
    window_seconds: int
    uptime_seconds: float
    summaries: dict[str, TelemetryDurationSummary]
    trends: list[TelemetryTrendPoint]
    recent_events: list[TelemetryEventResponse]
    metrics: list[TelemetryMetricResponse]
    rollups: list[dict[str, Any]] = Field(default_factory=list)


# === Backup/Restore Schemas ===


class BackupDataSchema(BaseModel):
    """Graph backup data structure."""

    version: str
    created_at: str
    organization_id: str
    entity_count: int
    relationship_count: int
    entities: list[dict]
    relationships: list[dict]


class BackupResponse(BaseModel):
    """Response from backup operation."""

    success: bool
    entity_count: int
    relationship_count: int
    message: str
    duration_seconds: float
    backup_data: BackupDataSchema | None = None


class RestoreRequest(BaseModel):
    """Request to restore from backup."""

    backup_data: BackupDataSchema
    skip_existing: bool = True


class RestoreResponse(BaseModel):
    """Response from restore operation."""

    success: bool
    entities_restored: int
    relationships_restored: int
    entities_skipped: int
    relationships_skipped: int
    errors: list[str]
    duration_seconds: float


class BackfillRequest(BaseModel):
    """Request to backfill missing relationships."""

    dry_run: bool = Field(
        default=False, description="If true, report what would be done without making changes"
    )


class BackfillResponse(BaseModel):
    """Response from relationship backfill operation."""

    success: bool
    relationships_created: int
    tasks_without_project: int
    tasks_already_linked: int
    errors: list[str]
    duration_seconds: float
    dry_run: bool


class ProjectRecordBackfillRequest(BaseModel):
    """Request to backfill missing project control-plane records."""

    dry_run: bool = Field(
        default=True, description="If true, report missing records without creating them"
    )


class ProjectRecordBackfillItem(BaseModel):
    """Per-project result from project record backfill."""

    graph_project_id: str
    status: Literal["existing", "would_create", "created", "skipped", "failed"]
    reason: str | None = None


class ProjectRecordBackfillResponse(BaseModel):
    """Response from project record backfill operation."""

    success: bool
    dry_run: bool
    existing: int
    would_create: int
    created: int
    skipped: int
    failed: int
    projects: list[ProjectRecordBackfillItem]
    errors: list[str]
    duration_seconds: float


class DebugQueryRequest(BaseModel):
    """Request for executing a read-only debug query."""

    cypher: str = Field(
        ...,
        description="Read-only graph query to execute (SurrealQL for Surreal runtime)",
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Query parameters")


class DebugQueryResponse(BaseModel):
    """Response from debug query execution."""

    rows: list[dict[str, Any]] = Field(default_factory=list, description="Query result rows")
    row_count: int = Field(default=0, description="Number of rows returned")
    error: str | None = Field(default=None, description="Error message if query failed")


class DevStatusResponse(BaseModel):
    """Comprehensive developer status dashboard."""

    # Component health
    api_healthy: bool = Field(description="API server is healthy")
    worker_healthy: bool = Field(description="Worker process is running")
    graph_healthy: bool = Field(description="Graph runtime is reachable")
    queue_healthy: bool = Field(description="Job queue is healthy")
    coordination_backend: str = Field(description="Resolved coordination backend")
    coordination_status: str = Field(description="Coordination subsystem status")
    coordination_durable: bool = Field(description="Coordination state survives process restarts")
    coordination_error: str | None = Field(
        default=None, description="Coordination error or readiness message"
    )

    # Stats
    uptime_seconds: float = Field(default=0, description="Server uptime")
    entity_count: int = Field(default=0, description="Total entities in graph")
    queue_depth: int = Field(default=0, description="Jobs in queue")

    # Recent activity
    recent_errors: list[dict[str, Any]] = Field(
        default_factory=list, description="Recent error log entries"
    )
    surreal_observability: dict[str, Any] = Field(
        default_factory=dict,
        description="SurrealDB HTTP health and metrics scrape availability",
    )
