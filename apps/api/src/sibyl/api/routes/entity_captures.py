"""Canonical captures ownership for entity routes."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Annotated, Any
from uuid import UUID, uuid4, uuid5

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
from sibyl_core.auth import AuthOrganization, ProjectRole
from sibyl_core.auth.memory_policy import CAPTURE_EXPIRES_AT_METADATA_KEY
from sibyl_core.memory_pipeline.source_lifecycle import (
    SOURCE_BINDINGS_KEY,
    UNKNOWN_SOURCE_REVISION,
    source_revision_bindings,
)

log = structlog.get_logger()

router = APIRouter(
    prefix="/entities",
    tags=["entities"],
    dependencies=[Depends(require_org_role(*contracts.READ_ROLES))],
)


# Namespace for sidecar ids derived from a request's idempotency key.
_SIDECAR_ID_NAMESPACE = UUID("10a0ef8c-2972-4676-9eb2-b9748d62d012")


def _claim_sidecar_id(
    *,
    organization_id: UUID,
    principal_id: str,
    raw_capture_id: str,
    idempotency_key: str | None,
) -> UUID:
    """The sidecar id a claim stamps, stable across retries of one request.

    A request interrupted after its claim leaves the raw row naming this id.
    Its retry carries the same idempotency key, derives the same id, and the
    claim accepts its own earlier stamp, so the retry binds and re-archives
    the sidecar in place rather than writing the row unbound. Without a key
    there is no retry to recognize, and every claim gets a fresh id.
    """
    if not idempotency_key:
        return uuid4()
    return uuid5(
        _SIDECAR_ID_NAMESPACE,
        "\x1f".join((str(organization_id), principal_id, raw_capture_id, idempotency_key)),
    )


@dataclass(frozen=True, slots=True)
class _ClaimedCapture:
    id: str
    revision: int
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SourceCaptureBinding:
    """A raw memory this request proved it may project, and the stamps it owes.

    ``provenance`` is what the graph row carries back to the capture, built
    from the stored raw row rather than from the request. ``sidecar_id`` is the
    id the archive sidecar must be written under, because the claim already
    points the raw row at it.
    """

    raw_capture_id: str
    principal_id: str
    sidecar_id: UUID
    provenance: dict[str, Any]


def _claimed_capture_provenance(
    claimed: Mapping[str, object], *, row_content: str
) -> dict[str, Any] | None:
    """The provenance MCP remember stamps, read off the raw row this request claimed.

    A binding says which revision of the capture's text the row was built
    from, so the claimed revision is bound only when the row carries the text
    the capture holds now. A buffered graph write that replays after the raw
    memory was revised carries the old text; bound at the new revision it
    would read as fresh. Every binding is unknown instead, which a revised
    capture treats as stale, so the row is born blocked rather than serving
    text its author already corrected. Both sides are compared stripped, the
    way the graph writer stores the body (``MemoryCaptureRequest.stored_content``).

    None when the stored row cannot support honest source bindings, where the
    MCP path would raise.
    """
    raw_memory_id = str(claimed.get("uuid") or "")
    metadata = claimed.get("metadata")
    stored_metadata: Mapping[str, object] = metadata if isinstance(metadata, Mapping) else {}
    revision = claimed.get("revision")
    if not isinstance(revision, int):
        return None
    try:
        bindings = source_revision_bindings(
            [_ClaimedCapture(id=raw_memory_id, revision=revision, metadata=stored_metadata)]
        )
    except ValueError:
        return None
    stored_text = claimed.get("raw_content")
    if not isinstance(stored_text, str) or stored_text.strip() != row_content.strip():
        bindings = dict.fromkeys(bindings, UNKNOWN_SOURCE_REVISION)
    provenance: dict[str, Any] = {
        "raw_memory_id": raw_memory_id,
        SOURCE_BINDINGS_KEY: bindings,
    }
    if source_id := claimed.get("source_id"):
        provenance["raw_source_id"] = str(source_id)
    if expires_at := stored_metadata.get(CAPTURE_EXPIRES_AT_METADATA_KEY):
        provenance[CAPTURE_EXPIRES_AT_METADATA_KEY] = expires_at
    return provenance


async def claim_source_capture(
    session: Any,
    *,
    organization_id: UUID,
    raw_capture_id: str,
    principal_id: str | None,
    graph_metadata: Mapping[str, Any],
    row_content: str,
    archives_capture: bool,
    idempotency_key: str | None = None,
) -> SourceCaptureBinding | None:
    """Prove a caller-named raw memory belongs behind this graph write.

    ``raw_memory_id`` in an entity body is a claim, not provenance. Binding it
    unproven is the attack the provenance strip exists to stop: a caller
    naming somebody else's capture, or one of its own in another audience,
    would arrange for a correction on that capture to retire this row. So the
    claim binds only when ``mark_raw_capture_projected`` stamps the raw row,
    which happens only for the caller's own raw memory in this organization,
    in exactly the scope and scope key the graph row is stamped with, and not
    yet projected anywhere. The stamp names the sidecar this request is about
    to archive, so a write that archives no sidecar has nothing to bind with.

    Every refusal still lets the row be written, unbound, and is logged. That
    includes a claim the store could not answer: two writes racing for one
    capture can lose a transaction conflict, and the loser still writes.
    """

    def refused(reason: str) -> None:
        log.warning(
            "entity_raw_capture_binding_refused",
            organization_id=str(organization_id),
            raw_memory_id=raw_capture_id,
            reason=reason,
        )

    if not archives_capture:
        return refused("not_a_capture_write")
    if not principal_id:
        return refused("no_authenticated_principal")
    sidecar_id = _claim_sidecar_id(
        organization_id=organization_id,
        principal_id=principal_id,
        raw_capture_id=raw_capture_id,
        idempotency_key=idempotency_key,
    )
    memory_scope = graph_metadata.get("memory_scope")
    scope_key = graph_metadata.get("scope_key")
    try:
        claimed = await content_runtime.mark_raw_capture_projected(
            session,
            organization_id=organization_id,
            raw_capture_id=raw_capture_id,
            projected_capture_id=sidecar_id,
            principal_id=principal_id,
            memory_scope=str(memory_scope) if memory_scope else "",
            scope_key=str(scope_key) if scope_key else None,
        )
    except Exception as exc:
        log.warning(
            "entity_raw_capture_claim_failed",
            organization_id=str(organization_id),
            raw_memory_id=raw_capture_id,
            error_type=type(exc).__name__,
        )
        return refused("claim_failed")
    if claimed is None:
        return refused("capture_not_claimable")
    binding = SourceCaptureBinding(
        raw_capture_id=raw_capture_id,
        principal_id=principal_id,
        sidecar_id=sidecar_id,
        provenance={},
    )
    provenance = _claimed_capture_provenance(claimed, row_content=row_content)
    if provenance is None:
        await release_source_capture(session, organization_id=organization_id, binding=binding)
        return refused("capture_bindings_malformed")
    return replace(binding, provenance=provenance)


async def release_source_capture(
    session: Any,
    *,
    organization_id: UUID,
    binding: SourceCaptureBinding,
) -> None:
    """Give a claimed raw memory back when the write it was claimed for failed.

    Best effort: the caller is already on a failure path, and a release that
    raises would replace the error the caller needs to see.
    """
    try:
        await content_runtime.release_raw_capture_projection(
            session,
            organization_id=organization_id,
            raw_capture_id=binding.raw_capture_id,
            projected_capture_id=binding.sidecar_id,
            principal_id=binding.principal_id,
        )
    except Exception as exc:
        log.warning(
            "entity_raw_capture_release_failed",
            organization_id=str(organization_id),
            raw_memory_id=binding.raw_capture_id,
            error_type=type(exc).__name__,
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
    project_id: str | None,
    capture_id: UUID | None = None,
) -> None:
    """Persist the write-once capture sidecar.

    The sidecar is private to its writer: it has no lifecycle of its own, so
    corrections to the memory never reach it, and raw recall would serve it
    to any wider audience. The graph row and the raw memory carry the
    audience. ``project_id`` is the project the route verified, which files
    the sidecar where its writer will look for it; ``metadata`` has every
    server-owned key stripped and cannot supply it.

    ``capture_id`` is the id a source capture claim already stamped onto the
    raw memory (see ``claim_source_capture``), which is what folds the raw
    row behind this sidecar in the review queue.
    """
    capture_surface_value = metadata.get("capture_surface")
    capture = RawCaptureRecord(
        organization_id=organization_id,
        principal_id=str(user_id) if user_id else "",
        project_id=project_id or None,
        entity_id=entity_id,
        title=entity_name,
        raw_content=entity_content,
        entity_type=entity_type,
        tags=tags,
        metadata=metadata,
        capture_surface=str(capture_surface_value) if capture_surface_value else None,
        created_by_user_id=user_id,
        **({"id": capture_id} if capture_id is not None else {}),
    )
    await save_raw_capture_record(session, capture=capture)


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
    project_ids: Annotated[list[str] | None, Query(min_length=1)] = None,
) -> RawCaptureListResponse:
    """List archived raw quick captures for the current organization."""
    accessible_projects = await policy.accessible_project_ids_for_read(ctx)
    if project_ids is not None:
        if not set(project_ids) <= accessible_projects:
            raise HTTPException(status_code=403, detail="project_scope_denied")
        for project_id in project_ids:
            await policy.verify_entity_project_access(
                None,
                ctx,
                project_id,
                required_role=ProjectRole.VIEWER,
                require_existing_project=True,
            )
    captures, has_more = await content_runtime.list_raw_captures(
        session,
        organization_id=org.id,
        entity_type=entity_type,
        capture_surface=capture_surface,
        review_state=review_state,
        limit=limit,
        offset=offset,
        private_owner=ctx.user_id,
        **({"project_ids": project_ids} if project_ids is not None else {}),
    )
    captures = [
        capture
        for capture in captures
        if (
            project_ids is None
            or (capture.project_id or capture.metadata.get("project_id")) in project_ids
        )
        and policy.raw_capture_visible_to_reader(
            capture,
            ctx=ctx,
            accessible_projects=accessible_projects,
        )
    ]

    return RawCaptureListResponse(
        captures=[serialization.serialize_raw_capture_summary(capture) for capture in captures],
        limit=limit,
        offset=offset,
        has_more=has_more,
        scope="project_selection" if project_ids is not None else "all_projects",
        project_ids=project_ids,
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
    capture = await content_runtime.get_raw_capture(
        session,
        organization_id=org.id,
        capture_id=capture_id,
    )
    if not capture or not policy.raw_capture_visible_to_reader(
        capture,
        ctx=ctx,
        accessible_projects=accessible_projects,
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
    """Update review-state metadata for a raw capture.

    Seeing a capture is not enough to move it: someone else's project
    capture takes project maintainer, as a correction does.
    """
    accessible_projects = await policy.accessible_project_ids_for_read(ctx)
    existing = await content_runtime.get_raw_capture(
        session,
        organization_id=org.id,
        capture_id=capture_id,
    )
    if not existing or not policy.raw_capture_visible_to_reader(
        existing,
        ctx=ctx,
        accessible_projects=accessible_projects,
    ):
        raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")
    decision = await policy.authorize_raw_capture_review(existing, ctx=ctx)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason)

    capture = await content_runtime.update_raw_capture_review_state(
        session,
        organization_id=org.id,
        capture_id=capture_id,
        review_state=update.review_state,
    )
    if not capture:
        raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")
    return serialization.serialize_raw_capture(capture)
