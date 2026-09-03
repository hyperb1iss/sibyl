"""Canonical captures ownership for entity routes."""

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.routes import (
    entity_contracts as contracts,
    entity_policy as policy,
    entity_serialization as serialization,
)
from sibyl.api.schemas import (
    RawCaptureListResponse,
    RawCaptureResponse,
    RawCaptureReviewUpdate,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_organization,
    require_org_role,
)
from sibyl.persistence import content_runtime
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl.persistence.content_runtime import (
    get_content_read_session_dependency,
    save_raw_capture_record,
)
from sibyl_core.auth import AuthOrganization

log = structlog.get_logger()

router = APIRouter(
    prefix="/entities",
    tags=["entities"],
    dependencies=[Depends(require_org_role(*contracts.READ_ROLES))],
)


async def archive_raw_capture(
    session: Any,
    *,
    organization_id: UUID,
    user_id: UUID | None,
    entity_id: str,
    entity_name: str,
    entity_content: str,
    entity_type: str,
    tags: list[str],
    metadata: dict[str, object],
) -> None:
    """Persist the write-once capture sidecar.

    When the entity is the projection of a raw memory (metadata carries
    raw_memory_id), the raw row is stamped with this capture's id so the
    review queue lists the memory once instead of raw and projection side
    by side.
    """
    capture_surface_value = metadata.get("capture_surface")
    capture = RawCaptureRecord(
        organization_id=organization_id,
        principal_id=str(user_id) if user_id else "",
        memory_scope=str(metadata.get("memory_scope") or "private"),
        scope_key=str(metadata["scope_key"]) if metadata.get("scope_key") else None,
        agent_id=str(metadata["agent_id"]) if metadata.get("agent_id") else None,
        project_id=str(metadata["project_id"]) if metadata.get("project_id") else None,
        review_state=serialization.normalized_raw_capture_review_state(
            metadata.get("review_state")
        ),
        entity_id=entity_id,
        title=entity_name,
        raw_content=entity_content,
        entity_type=entity_type,
        tags=tags,
        metadata=metadata,
        capture_surface=str(capture_surface_value) if capture_surface_value else None,
        created_by_user_id=user_id,
    )
    await save_raw_capture_record(session, capture=capture)
    raw_memory_id = metadata.get("raw_memory_id")
    if raw_memory_id:
        await content_runtime.mark_raw_capture_projected(
            session,
            organization_id=organization_id,
            raw_capture_id=str(raw_memory_id),
            projected_capture_id=capture.id,
            principal_id=capture.principal_id,
        )


@router.get("/captures", response_model=RawCaptureListResponse)
@handle_workflow_errors("list_raw_captures")
async def list_raw_captures(
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: Any = Depends(get_content_read_session_dependency),
    entity_type: str | None = Query(default=None, description="Filter by entity type"),
    capture_surface: str | None = Query(default=None, description="Filter by capture surface"),
    review_state: str | None = Query(default=None, description="Filter by review queue state"),
    limit: int = Query(default=50, ge=1, le=200, description="Items per page"),
    offset: int = Query(default=0, ge=0, description="Results to skip"),
) -> RawCaptureListResponse:
    """List archived raw quick captures for the current organization."""
    accessible_projects = await policy.accessible_project_ids_for_read(ctx)
    accessible_delegations = await policy.accessible_delegation_scope_keys_for_read(ctx)
    captures, has_more = await content_runtime.list_raw_captures(
        session,
        organization_id=org.id,
        entity_type=entity_type,
        capture_surface=capture_surface,
        review_state=review_state,
        limit=limit,
        offset=offset,
    )
    captures = [
        capture
        for capture in captures
        if policy.raw_capture_visible_to_reader(
            capture,
            ctx=ctx,
            accessible_projects=accessible_projects,
            accessible_delegations=accessible_delegations,
        )
    ]

    return RawCaptureListResponse(
        captures=[serialization.serialize_raw_capture_summary(capture) for capture in captures],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/captures/{capture_id}", response_model=RawCaptureResponse)
@handle_workflow_errors("get_raw_capture", id_param="capture_id")
async def get_raw_capture(
    capture_id: UUID,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: Any = Depends(get_content_read_session_dependency),
) -> RawCaptureResponse:
    """Get a single archived raw quick capture."""
    accessible_projects = await policy.accessible_project_ids_for_read(ctx)
    accessible_delegations = await policy.accessible_delegation_scope_keys_for_read(ctx)
    capture = await content_runtime.get_raw_capture(
        session,
        organization_id=org.id,
        capture_id=capture_id,
    )
    if not capture or not policy.raw_capture_visible_to_reader(
        capture,
        ctx=ctx,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
    ):
        raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")

    return serialization.serialize_raw_capture(capture)


@router.patch(
    "/captures/{capture_id}",
    response_model=RawCaptureResponse,
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)
@handle_workflow_errors("update_raw_capture_review_state", id_param="capture_id")
async def update_raw_capture_review_state(
    capture_id: UUID,
    update: RawCaptureReviewUpdate,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: Any = Depends(get_content_read_session_dependency),
) -> RawCaptureResponse:
    """Update review-state metadata for a raw capture."""
    accessible_projects = await policy.accessible_project_ids_for_read(ctx)
    accessible_delegations = await policy.accessible_delegation_scope_keys_for_read(ctx)
    existing = await content_runtime.get_raw_capture(
        session,
        organization_id=org.id,
        capture_id=capture_id,
    )
    if not existing or not policy.raw_capture_visible_to_reader(
        existing,
        ctx=ctx,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
    ):
        raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")

    capture = await content_runtime.update_raw_capture_review_state(
        session,
        organization_id=org.id,
        capture_id=capture_id,
        review_state=update.review_state,
    )
    if not capture:
        raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")
    return serialization.serialize_raw_capture(capture)
