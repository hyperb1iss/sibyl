"""Runtime telemetry endpoints."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from sibyl.api.schemas import TelemetrySummaryResponse
from sibyl.auth.dependencies import require_org_role
from sibyl.coordination import get_coordination_health
from sibyl.services.telemetry import (
    list_runtime_rollups,
    maybe_persist_runtime_rollup,
    runtime_summary,
)
from sibyl_core.auth import OrganizationRole
from sibyl_core.observability import telemetry_registry

_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)

router = APIRouter(
    prefix="/telemetry",
    tags=["telemetry"],
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)


@router.get("/summary", response_model=TelemetrySummaryResponse)
async def telemetry_summary(
    window_seconds: int = Query(default=900, ge=60, le=86_400),
    rollup_limit: int = Query(default=120, ge=0, le=1_440),
) -> TelemetrySummaryResponse:
    """Return runtime performance summaries and recent persisted rollups."""
    with contextlib.suppress(Exception):
        health = await get_coordination_health()
        telemetry_registry().record_queue_health(
            backend=str(health.get("queue_backend") or health.get("backend") or "unknown"),
            queue_depth=int(health.get("queue_depth") or 0),
            queue_healthy=bool(health.get("queue_healthy")),
            worker_healthy=bool(health.get("worker_healthy")),
        )

    with contextlib.suppress(Exception):
        await maybe_persist_runtime_rollup(window_seconds=window_seconds)

    summary = runtime_summary(window_seconds=window_seconds)
    rollups = await list_runtime_rollups(limit=rollup_limit) if rollup_limit else []
    return TelemetrySummaryResponse(**summary, rollups=rollups)


@router.get("/prometheus", response_class=PlainTextResponse)
async def telemetry_prometheus() -> PlainTextResponse:
    """Return Prometheus-compatible runtime metrics."""
    return PlainTextResponse(
        telemetry_registry().prometheus_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
