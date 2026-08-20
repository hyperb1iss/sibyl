"""Memory source inspection, blame, and correction routes."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.idempotency import (
    mutation_receipt,
    replay_idempotent_response,
    save_idempotent_response,
    serialize_idempotent_request,
)
from sibyl.api.routes import memory_auth, memory_serialization as serialization
from sibyl.api.schemas import (
    MemoryCorrectionRequest,
    MemoryCorrectionResponse,
    MemorySourceBlameResponse,
    MemorySourceInspectResponse,
    SourceImportStatusResponse,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.jobs.source_imports import get_source_import_status
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
)
from sibyl_core.services.memory import (
    apply_memory_correction,
    preview_memory_correction,
)
from sibyl_core.services.surreal_content import (
    get_raw_memory_lineage,
)

log = structlog.get_logger()

_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)
_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)
_ARCHIVEABLE_REFLECTION_EXCEPTION_REASONS = frozenset(
    {
        "duplicate_candidate",
        "stale_candidate",
    }
)


router = APIRouter(prefix="/memory", tags=["memory"])


@router.get(
    "/source-imports/{import_id:path}",
    response_model=SourceImportStatusResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
async def get_memory_source_import_status(
    import_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SourceImportStatusResponse:
    """Get source-safe import progress from the memory surface."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = await get_source_import_status(
            import_id,
            organization_id=str(org.id),
            principal_id=principal_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="source_import_not_found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="source_import_forbidden") from exc
    return SourceImportStatusResponse.model_validate(payload)


@router.get(
    "/inspect/{source_id:path}",
    response_model=MemorySourceInspectResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
@handle_workflow_errors("inspect_memory_source", id_param="source_id")
async def inspect_memory_source(
    source_id: str,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemorySourceInspectResponse:
    """Inspect a raw memory source and its audit-derived records."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    memory = await memory_auth.load_memory_source_for_org(
        organization_id=str(org.id),
        source_id=source_id,
    )
    policy_decision = await memory_auth.inspect_content_policy(ctx=ctx, memory=memory)
    audit_events = await serialization.source_audit_events(
        organization_id=str(org.id),
        source_id=source_id,
        memory=memory,
    )
    response = serialization.memory_source_inspect_response(
        memory=memory,
        policy_decision=policy_decision,
        audit_events=audit_events,
    )
    await memory_auth.log_memory_audit(
        action="memory.inspect",
        ctx=ctx,
        request=http_request,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=response.project_id,
        source_surface="memory_inspect",
        source_ids=[memory.id, memory.source_id],
        derived_ids=response.derived_ids,
        policy_allowed=policy_decision.allowed,
        policy_reason=policy_decision.reason,
        details={
            "audit_event_count": response.audit_event_count,
            "content_redacted": response.content_redacted,
        },
    )
    return response


@router.get(
    "/blame/{source_id:path}",
    response_model=MemorySourceBlameResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
@handle_workflow_errors("blame_memory_source", id_param="source_id")
async def blame_memory_source(
    source_id: str,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemorySourceBlameResponse:
    """Inspect revision, correction, audit, and lineage history for a memory."""
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    memory = await memory_auth.load_memory_source_for_org(
        organization_id=str(org.id),
        source_id=source_id,
    )
    policy_decision = await memory_auth.require_source_policy(
        ctx=ctx,
        memory=memory,
        action=MemoryPolicyAction.READ,
        surface="memory_blame",
        request=http_request,
    )
    audit_events = await serialization.source_audit_events(
        organization_id=str(org.id),
        source_id=source_id,
        memory=memory,
    )
    source = serialization.memory_source_inspect_response(
        memory=memory,
        policy_decision=policy_decision,
        audit_events=audit_events,
    )
    lineage = await get_raw_memory_lineage(
        organization_id=str(org.id),
        memory_id=memory.id,
    )
    response = MemorySourceBlameResponse(
        source=source,
        content_revisions=(
            []
            if source.content_redacted
            else serialization.metadata_dicts(memory.metadata.get("content_revisions"))
        ),
        derived_from=lineage["derived_from"],
        supersessions=lineage["supersessions"],
    )
    await memory_auth.log_memory_audit(
        action="memory.blame",
        ctx=ctx,
        request=http_request,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=source.project_id,
        source_surface="memory_blame",
        source_ids=[memory.id, memory.source_id],
        derived_ids=source.derived_ids,
        policy_allowed=True,
        policy_reason=policy_decision.reason,
        details={
            "content_revision_count": len(response.content_revisions),
            "derived_from_count": len(response.derived_from),
            "supersession_count": len(response.supersessions),
        },
    )
    return response


@router.post(
    "/inspect/{source_id:path}/corrections/preview",
    response_model=MemoryCorrectionResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def preview_memory_correction_route(
    source_id: str,
    request: MemoryCorrectionRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemoryCorrectionResponse:
    """Preview a memory correction or lifecycle action without mutating."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    memory = await memory_auth.load_memory_source_for_org(
        organization_id=str(org.id),
        source_id=source_id,
    )
    await memory_auth.require_source_policy(
        ctx=ctx,
        memory=memory,
        action=MemoryPolicyAction.WRITE,
        surface="memory_correction_preview",
        request=http_request,
    )
    accessible_projects = await memory_auth.project_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    accessible_teams = await memory_auth.team_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    preview = await preview_memory_correction(
        organization_id=str(org.id),
        source_id=memory.id,
        principal_id=principal_id,
        action=request.action,
        reason=request.reason,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        replacement_source_id=request.replacement_source_id,
        duplicate_of_source_id=request.duplicate_of_source_id,
        revised_content=request.revised_content,
    )
    response = serialization.correction_response(preview)
    await memory_auth.log_memory_audit(
        action=f"{preview.audit_action}.preview",
        ctx=ctx,
        request=http_request,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=serialization.memory_project_id(memory),
        source_surface="memory_correction_preview",
        source_ids=preview.affected_source_ids or [memory.id],
        derived_ids=preview.affected_derived_ids,
        policy_allowed=preview.allowed,
        policy_reason=preview.reason,
        details={
            "action": preview.action,
            "metadata": dict(request.metadata),
            "recall_impact": dict(preview.recall_impact),
            "synthesis_impact": dict(preview.synthesis_impact),
            "target_lifecycle_state": preview.target_lifecycle_state,
            "target_lifecycle_flags": preview.target_lifecycle_flags,
        },
    )
    return response


@router.post(
    "/inspect/{source_id:path}/corrections",
    response_model=MemoryCorrectionResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("apply_memory_correction", id_param="source_id")
@serialize_idempotent_request
async def apply_memory_correction_route(
    source_id: str,
    request: MemoryCorrectionRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemoryCorrectionResponse:
    """Apply a memory correction or lifecycle action."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    memory = await memory_auth.load_memory_source_for_org(
        organization_id=str(org.id),
        source_id=source_id,
    )
    await memory_auth.require_source_policy(
        ctx=ctx,
        memory=memory,
        action=MemoryPolicyAction.WRITE,
        surface="memory_correction",
        request=http_request,
    )
    accessible_projects = await memory_auth.project_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    accessible_teams = await memory_auth.team_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    idempotency_path = f"/memory/inspect/{source_id}/corrections"
    idempotency_payload = {"body": request.model_dump(mode="json")}
    replayed = await replay_idempotent_response(
        http_request,
        organization_id=org.id,
        principal_id=principal_id,
        method="POST",
        path=idempotency_path,
        payload=idempotency_payload,
        response_model=MemoryCorrectionResponse,
        content_session=None,
    )
    if replayed is not None:
        return replayed
    correction_kwargs: dict[str, Any] = {
        "organization_id": str(org.id),
        "source_id": memory.id,
        "principal_id": principal_id,
        "action": request.action,
        "reason": request.reason,
        "accessible_projects": accessible_projects,
        "accessible_teams": accessible_teams,
        "replacement_source_id": request.replacement_source_id,
        "duplicate_of_source_id": request.duplicate_of_source_id,
        "revised_content": request.revised_content,
    }
    if request.expected_revision is not None:
        correction_kwargs["expected_revision"] = request.expected_revision
    result = await apply_memory_correction(
        **correction_kwargs,
    )
    updated_revision = result.updated_memory.revision if result.updated_memory else None
    response = serialization.correction_result_response(
        result,
        receipt=mutation_receipt(
            http_request,
            applied=result.applied,
            revision=updated_revision,
            affected_records=(
                [
                    *(
                        f"raw_captures:{affected_id}"
                        for affected_id in result.preview.affected_source_ids
                    ),
                    # The graph rows are named too, because they are what
                    # retrieval ranks: a receipt listing only the capture
                    # would still read as though the correction stopped there.
                    *(f"entity:{entity_id}" for entity_id in result.affected_entity_ids),
                ]
                if result.applied
                else []
            ),
        ),
    )
    await memory_auth.log_memory_audit(
        action=result.preview.audit_action,
        ctx=ctx,
        request=http_request,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=serialization.memory_project_id(memory),
        source_surface="memory_correction",
        source_ids=result.preview.affected_source_ids or [memory.id],
        derived_ids=result.preview.affected_derived_ids,
        policy_allowed=result.preview.allowed and result.applied,
        policy_reason=result.preview.reason,
        details={
            "action": result.preview.action,
            "applied": result.applied,
            "metadata": dict(request.metadata),
            "recall_impact": dict(result.preview.recall_impact),
            "synthesis_impact": dict(result.preview.synthesis_impact),
            "target_lifecycle_state": result.preview.target_lifecycle_state,
            "target_lifecycle_flags": result.preview.target_lifecycle_flags,
            "updated_review_state": response.updated_review_state,
        },
    )
    await save_idempotent_response(
        http_request,
        organization_id=org.id,
        principal_id=principal_id,
        method="POST",
        path=idempotency_path,
        payload=idempotency_payload,
        response=response,
        status_code=200,
        content_session=None,
    )
    return response
