"""Raw memory remember, recall, audit, and citation routes."""

from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sibyl.api.idempotency import (
    mutation_receipt,
    replay_idempotent_response,
    save_idempotent_response,
    serialize_idempotent_request,
)
from sibyl.api.raw_capture_events import publish_raw_capture_changed
from sibyl.api.routes import memory_auth, memory_serialization as serialization
from sibyl.api.schemas import (
    MemoryAuditListResponse,
    MemoryCitationRequest,
    MemoryCitationResponse,
    RawMemoryRecallRequest,
    RawMemoryRecallResponse,
    RawMemoryRememberRequest,
    RawMemoryResponse,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import (
    list_memory_audit_events,
)
from sibyl.services.recall_limits import (
    RecallConcurrencyLimitExceededError,
    recall_concurrency_slot,
)
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
)
from sibyl_core.observability import elapsed_ms, telemetry_registry
from sibyl_core.services.surreal_content import (
    AGENT_DIARY_CAPTURE_SURFACE,
    RawMemoryRecallResult,
    recall_raw_memory_with_sources as recall_raw_memory,
    remember_raw_memory,
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


@router.post(
    "/raw",
    response_model=RawMemoryResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@serialize_idempotent_request
async def remember_raw(
    request: RawMemoryRememberRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> RawMemoryResponse:
    """Store verbatim memory before extraction or graph reflection."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    started_at = time.perf_counter()
    try:
        capture_surface = AGENT_DIARY_CAPTURE_SURFACE if request.diary else request.capture_surface
        source_id = request.source_id or f"{capture_surface}:manual"
        memory_auth.validate_diary_request(
            diary=request.diary,
            agent_id=request.agent_id,
            memory_scope=request.memory_scope,
        )
        await memory_auth.authorize_project_filter(
            ctx=ctx,
            project_id=request.project_id,
            required_project_role=ProjectRole.CONTRIBUTOR,
            surface="raw_remember",
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            policy_action="write",
            request=http_request,
        )
        await memory_auth.authorize_project_scope_write(
            ctx=ctx,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
        )
        write_decision = await memory_auth.authorize_memory_policy(
            ctx=ctx,
            action=MemoryPolicyAction.WRITE,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            surface="raw_remember",
            request=http_request,
            project_id=request.project_id,
        )
        idempotency_payload = {"body": request.model_dump(mode="json")}
        replayed = await replay_idempotent_response(
            http_request,
            organization_id=org.id,
            principal_id=principal_id,
            method="POST",
            path="/memory/raw",
            payload=idempotency_payload,
            response_model=RawMemoryResponse,
            content_session=None,
        )
        if replayed is not None:
            telemetry_registry().record_memory_operation(
                operation="remember_raw",
                status="ok",
                duration_ms=elapsed_ms(started_at),
                result_count=1,
            )
            return replayed
        metadata = memory_auth.diary_metadata(
            metadata=request.metadata,
            diary=request.diary,
            agent_id=request.agent_id,
            project_id=request.project_id,
        )
        memory = await remember_raw_memory(
            organization_id=str(org.id),
            principal_id=principal_id,
            source_id=source_id,
            raw_content=request.raw_content,
            title=request.title,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            tags=request.tags,
            metadata=metadata,
            provenance=request.provenance,
            capture_surface=capture_surface,
        )
        await memory_auth.log_memory_audit(
            action="memory.remember",
            ctx=ctx,
            request=http_request,
            memory_scope=memory.memory_scope.value,
            scope_key=memory.scope_key,
            project_id=request.project_id,
            source_surface=capture_surface,
            source_ids=[memory.source_id],
            derived_ids=[memory.id],
            policy_allowed=write_decision.allowed,
            policy_reason=write_decision.reason,
            details={
                "agent_id": request.agent_id,
                "capture_flags": {
                    "basis": metadata.get("basis"),
                    "pinned": metadata.get("pinned"),
                    "proposed_scope": metadata.get("suggested_memory_scope"),
                },
                "diary": request.diary,
                "tag_count": len(request.tags),
            },
        )
        await publish_raw_capture_changed(
            organization_id=memory.organization_id,
            raw_memory_ids=[memory.id],
        )
        response = serialization.raw_memory_response(
            memory,
            policy_reason=write_decision.reason,
            receipt=mutation_receipt(
                http_request,
                applied=True,
                revision=memory.revision,
                affected_records=[f"raw_captures:{memory.id}"],
            ),
        )
        await save_idempotent_response(
            http_request,
            organization_id=org.id,
            principal_id=principal_id,
            method="POST",
            path="/memory/raw",
            payload=idempotency_payload,
            response=response,
            status_code=200,
            content_session=None,
        )
        telemetry_registry().record_memory_operation(
            operation="remember_raw",
            status="ok",
            duration_ms=elapsed_ms(started_at),
            result_count=1,
        )
        return response
    except ValueError as e:
        telemetry_registry().record_memory_operation(
            operation="remember_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        telemetry_registry().record_memory_operation(
            operation="remember_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise
    except Exception as e:
        telemetry_registry().record_memory_operation(
            operation="remember_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        log.exception("remember_raw_memory_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to remember raw memory.") from e


@router.post(
    "/raw/recall",
    response_model=RawMemoryRecallResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
async def recall_raw(
    request: RawMemoryRecallRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> RawMemoryRecallResponse:
    """Recall verbatim memories through scoped retrieval."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    started_at = time.perf_counter()
    try:
        memory_auth.validate_diary_request(
            diary=request.diary,
            agent_id=request.agent_id,
            memory_scope=request.memory_scope,
        )
        read_decision = await memory_auth.authorize_memory_policy(
            ctx=ctx,
            action=MemoryPolicyAction.READ,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            surface="raw_recall",
            request=http_request,
            agent_id=request.agent_id,
            project_id=request.project_id,
        )
        await memory_auth.authorize_project_filter(
            ctx=ctx,
            project_id=request.project_id,
            required_project_role=ProjectRole.VIEWER,
            surface="raw_recall",
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            policy_action="read",
            request=http_request,
        )
        async with recall_concurrency_slot(
            organization_id=str(org.id),
            user_id=principal_id,
            organization_role=ctx.org_role,
        ):
            recall_kwargs: dict[str, Any] = {}
            if request.participants:
                recall_kwargs["participants"] = request.participants
            if request.labels:
                recall_kwargs["labels"] = request.labels
            if request.thread_id:
                recall_kwargs["thread_id"] = request.thread_id
            if request.occurred_after:
                recall_kwargs["occurred_after"] = request.occurred_after
            if request.occurred_before:
                recall_kwargs["occurred_before"] = request.occurred_before
            if request.as_of:
                recall_kwargs["as_of"] = request.as_of
            recall_result = await recall_raw_memory(
                organization_id=str(org.id),
                principal_id=principal_id,
                query=request.query,
                memory_scope=request.memory_scope,
                scope_key=request.scope_key,
                agent_id=request.agent_id,
                project_id=request.project_id,
                limit=request.limit,
                **recall_kwargs,
            )
            if isinstance(recall_result, RawMemoryRecallResult):
                memories = list(recall_result.memories)
                source_failures = [failure.as_metadata() for failure in recall_result.failures]
            else:
                memories = recall_result
                source_failures = []
        await memory_auth.log_memory_audit(
            action="memory.recall",
            ctx=ctx,
            request=http_request,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            project_id=request.project_id,
            source_surface="raw_recall",
            source_ids=[memory.source_id for memory in memories],
            derived_ids=[memory.id for memory in memories],
            policy_allowed=read_decision.allowed,
            policy_reason=read_decision.reason,
            details=memory_auth.raw_recall_audit_details(request, result_count=len(memories)),
        )
        response = RawMemoryRecallResponse(
            query=request.query,
            limit=request.limit,
            memories=[
                serialization.raw_memory_response(memory, policy_reason=read_decision.reason)
                for memory in memories
            ],
            policy_reason=read_decision.reason,
            source_degraded=bool(source_failures),
            source_failure_count=len(source_failures),
            source_failures=source_failures,
        )
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="ok",
            duration_ms=elapsed_ms(started_at),
            result_count=len(response.memories),
        )
        return response
    except ValueError as e:
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RecallConcurrencyLimitExceededError as e:
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="rate_limited",
            duration_ms=elapsed_ms(started_at),
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "recall_concurrency_limit_exceeded",
                "max_concurrent": e.max_concurrent,
            },
        ) from e
    except HTTPException:
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise
    except Exception as e:
        telemetry_registry().record_memory_operation(
            operation="recall_raw",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        log.exception("recall_raw_memory_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to recall raw memory.") from e


@router.get(
    "/audit",
    response_model=MemoryAuditListResponse,
    dependencies=[Depends(require_org_role(*_ADMIN_ROLES))],
)
async def list_memory_audit(
    org: AuthOrganization = Depends(get_current_organization),
    action: str | None = Query(default=None, description="Filter by audit action"),
    actor_user_id: str | None = Query(default=None, description="Filter by actor user ID"),
    source_id: str | None = Query(default=None, description="Filter by source ID"),
    derived_id: str | None = Query(default=None, description="Filter by derived ID"),
    memory_scope: str | None = Query(default=None, description="Filter by memory scope"),
    project_id: str | None = Query(default=None, description="Filter by project ID"),
    policy_allowed: bool | None = Query(default=None, description="Filter by policy state"),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum audit events"),
) -> MemoryAuditListResponse:
    """List memory audit events for owner/admin inspection."""
    memory_auth.validate_memory_audit_action(action)
    rows = await list_memory_audit_events(
        organization_id=org.id,
        user_id=actor_user_id,
        action=action,
        source_id=source_id,
        derived_id=derived_id,
        memory_scope=memory_scope,
        project_id=project_id,
        policy_allowed=policy_allowed,
        limit=limit,
    )
    return MemoryAuditListResponse(
        events=[serialization.audit_event_response(row) for row in rows],
        limit=limit,
    )


@router.post(
    "/cite",
    response_model=MemoryCitationResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def cite_memory(
    request: MemoryCitationRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> MemoryCitationResponse:
    """Record memories that materially informed or misled an answer or action."""
    from sibyl_core.tools.usage_citation import record_cited_item_usages

    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if request.project_id:
        await verify_entity_project_access(
            None,
            ctx,
            request.project_id,
            required_role=ProjectRole.VIEWER,
        )

    usage = await record_cited_item_usages(
        request.cited_ids,
        organization_id=str(org.id),
        principal_id=principal_id,
        project_id=request.project_id,
        source_surface=request.source_surface,
        request_metadata={
            "route": "memory_cite",
            "metadata": request.metadata,
        },
        misled=request.misled,
    )
    audit_action = "memory.misled" if request.misled else "memory.cite"
    policy_reason = "misled_recorded" if request.misled else "citation_recorded"
    await memory_auth.log_memory_audit(
        action=audit_action,
        ctx=ctx,
        request=http_request,
        memory_scope="project" if request.project_id else None,
        scope_key=request.project_id,
        source_surface=request.source_surface,
        policy_allowed=True,
        policy_reason=policy_reason,
        project_id=request.project_id,
        source_ids=request.cited_ids,
        details={"usage": usage, "metadata": request.metadata, "misled": request.misled},
    )
    return MemoryCitationResponse(cited_ids=request.cited_ids, usage=usage)
