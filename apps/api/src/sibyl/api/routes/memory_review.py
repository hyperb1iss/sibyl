"""Reflection autonomy and review-drain routes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from sibyl.api.routes import memory_auth, memory_serialization as serialization
from sibyl.api.schemas import (
    ReflectionAutonomyRequest,
    ReflectionAutonomyResponse,
    ReflectionReviewDrainItem,
    ReflectionReviewDrainRequest,
    ReflectionReviewDrainResponse,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl_core.auth import AuthOrganization, MemoryPolicyContext, OrganizationRole
from sibyl_core.auth.memory_policy import (
    authorize_memory_read,
)
from sibyl_core.services.memory import (
    ReflectionPromotionResult,
    preview_reflection_candidate_promotion,
    promote_reflection_candidate_review,
)
from sibyl_core.services.memory_autonomy import (
    ReflectionAutonomyOutcome,
    ReflectionAutonomyPolicy,
    decide_reflection_candidate_autonomy,
)
from sibyl_core.services.surreal_content import (
    RawMemory,
    list_reflection_candidate_reviews,
    save_raw_memory,
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


def _should_archive_reflection_exception(
    response: ReflectionAutonomyResponse,
    *,
    archive_reasons: set[str],
) -> bool:
    if response.outcome != "exception":
        return False
    exception_reasons = {str(reason) for reason in response.exception_reasons if str(reason)}
    if not exception_reasons:
        return False
    return bool(exception_reasons & archive_reasons) and exception_reasons <= archive_reasons


async def _archive_reflection_exception_candidate(
    *,
    response: ReflectionAutonomyResponse,
    org: AuthOrganization,
    ctx: AuthContext,
    request: Request | None,
    project_id: str | None,
) -> RawMemory | None:
    memory = await memory_auth.get_raw_memory(
        organization_id=str(org.id),
        memory_id=response.candidate_id,
    )
    if memory is None:
        return None
    archived_at = datetime.now(UTC).isoformat()
    metadata = {
        **memory.metadata,
        "review_state": "archived",
        "archived_at": archived_at,
        "archive_reason": response.reason,
        "archive_reasons": list(response.exception_reasons),
        "autonomy_outcome": response.outcome,
        "autonomy_recommended_action": response.recommended_action,
    }
    updated = await save_raw_memory(
        replace(
            memory,
            review_state="archived",
            metadata=metadata,
        )
    )
    await memory_auth.log_memory_audit(
        action="memory.reflect.auto_archive",
        ctx=ctx,
        request=request,
        memory_scope=response.promote_to_scope,
        scope_key=response.promote_to_scope_key,
        project_id=project_id,
        source_surface="reflection_auto_review",
        source_ids=[response.candidate_id, *response.raw_source_ids],
        derived_ids=[],
        policy_allowed=response.preview.allowed,
        policy_reason=response.reason,
        details={
            "archive_reasons": list(response.exception_reasons),
            "dry_run": False,
            "outcome": response.outcome,
            "recommended_action": response.recommended_action,
            "review_state": "archived",
        },
    )
    return updated


@router.post(
    "/reflection/review/auto",
    response_model=ReflectionAutonomyResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def auto_review_reflection_candidate(
    request: ReflectionAutonomyRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionAutonomyResponse:
    """Automatically review and promote safe reflection candidates."""
    try:
        return await _auto_review_reflection_candidate(
            request=request,
            http_request=http_request,
            org=org,
            ctx=ctx,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception(
            "auto_review_reflection_candidate_failed",
            candidate_id=request.candidate_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to auto-review reflection candidate.",
        ) from e


async def _auto_review_reflection_candidate(
    *,
    request: ReflectionAutonomyRequest,
    http_request: Request,
    org: AuthOrganization,
    ctx: AuthContext,
    accessible_projects: set[str] | None = None,
) -> ReflectionAutonomyResponse:
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if accessible_projects is None:
        accessible_projects = await memory_auth.accessible_projects_for_promotion(
            ctx=ctx,
            request=request,
            http_request=http_request,
        )
    preview = await preview_reflection_candidate_promotion(
        candidate_id=request.candidate_id,
        organization_id=str(org.id),
        principal_id=principal_id,
        promote_to_scope=request.promote_to_scope,
        promote_to_scope_key=request.promote_to_scope_key,
        domain=request.domain,
        project=request.project,
        accessible_projects=accessible_projects,
    )
    confidence_threshold = (
        request.confidence_threshold
        if request.confidence_threshold is not None
        else ReflectionAutonomyPolicy().confidence_threshold
    )
    decision = decide_reflection_candidate_autonomy(
        preview,
        policy=ReflectionAutonomyPolicy(confidence_threshold=confidence_threshold),
        dry_run=request.dry_run,
    )
    promotion: ReflectionPromotionResult | None = None
    if decision.should_promote:
        promotion = await promote_reflection_candidate_review(
            candidate_id=request.candidate_id,
            organization_id=str(org.id),
            principal_id=principal_id,
            promote_to_scope=request.promote_to_scope,
            promote_to_scope_key=request.promote_to_scope_key,
            domain=request.domain,
            project=request.project,
            related_to=request.related_to,
            accessible_projects=accessible_projects,
        )

    audit_action = (
        "memory.reflect.auto_promote"
        if decision.outcome is ReflectionAutonomyOutcome.AUTO_PROMOTE
        else "memory.reflect.auto_review"
    )
    audit_scope = decision.memory_scope.value if decision.memory_scope else request.promote_to_scope
    audit_scope_key = decision.scope_key or request.promote_to_scope_key
    await memory_auth.log_memory_audit(
        action=audit_action,
        ctx=ctx,
        request=http_request,
        memory_scope=audit_scope,
        scope_key=audit_scope_key,
        project_id=request.project,
        source_surface="reflection_auto_review",
        source_ids=[request.candidate_id, *decision.raw_source_ids],
        derived_ids=[promotion.promoted_id] if promotion and promotion.promoted_id else [],
        policy_allowed=preview.allowed,
        policy_reason=decision.reason,
        details={
            "action_succeeded": promotion.success if promotion else False,
            "confidence": decision.confidence,
            "confidence_threshold": decision.confidence_threshold,
            "domain": request.domain,
            "dry_run": request.dry_run,
            "exception_reasons": decision.exception_reasons,
            "outcome": decision.outcome.value,
            "recommended_action": decision.recommended_action.value,
            "related_to_count": len(request.related_to),
            "review_state": promotion.review_state if promotion else decision.review_state,
            "source_count": len(decision.raw_source_ids),
        },
    )
    if preview.reason == "candidate_not_found":
        raise HTTPException(
            status_code=404,
            detail="reflection_candidate_not_found",
        )
    return serialization.autonomy_response(decision=decision, preview=preview, promotion=promotion)


@router.post(
    "/reflection/review/drain",
    response_model=ReflectionReviewDrainResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def drain_reflection_review(
    request: ReflectionReviewDrainRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionReviewDrainResponse:
    """Bulk auto-review pending reflection candidates."""
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    archive_reasons = {
        reason
        for reason in request.archive_exception_reasons
        if reason in _ARCHIVEABLE_REFLECTION_EXCEPTION_REASONS
    }
    try:
        candidates = await list_reflection_candidate_reviews(
            organization_id=str(org.id),
            review_state="pending",
            limit=request.limit,
        )
        if not candidates:
            return serialization.drain_response(request=request, results=[])
        accessible_projects = await memory_auth.accessible_projects_for_promotion(
            ctx=ctx,
            request=ReflectionAutonomyRequest(
                candidate_id="reflection-review-drain",
                promote_to_scope=request.promote_to_scope,
                promote_to_scope_key=request.promote_to_scope_key,
                domain=request.domain,
                project=request.project,
                related_to=request.related_to,
                dry_run=request.dry_run,
                confidence_threshold=request.confidence_threshold,
            ),
            http_request=http_request,
        )
        source_accessible_projects = await memory_auth.list_accessible_project_graph_ids(ctx)
        readable_projects = {str(project_id) for project_id in source_accessible_projects or set()}
        results: list[ReflectionReviewDrainItem] = []
        for candidate in candidates:
            policy_context = MemoryPolicyContext(
                actor_user_id=ctx.user_id,
                organization_id=ctx.organization_id,
                organization_role=ctx.org_role,
                memory_space=candidate.memory_scope.value,
                scope_key=candidate.scope_key,
                project_id=serialization.memory_project_id(candidate),
                accessible_projects=readable_projects,
                agent_id=candidate.agent_id,
                source_surface="reflection_review_drain",
            )
            decision = authorize_memory_read(policy_context=policy_context)
            if not decision.allowed:
                results.append(
                    ReflectionReviewDrainItem(
                        candidate_id=candidate.id,
                        outcome="skip",
                        recommended_action="route_to_review",
                        dry_run=request.dry_run,
                        reason="policy_denied",
                        review_state=candidate.review_state,
                        raw_source_ids=[],
                        policy_reasons=[decision.reason],
                    )
                )
                continue
            candidate_request = ReflectionAutonomyRequest(
                candidate_id=candidate.id,
                promote_to_scope=request.promote_to_scope,
                promote_to_scope_key=request.promote_to_scope_key,
                domain=request.domain,
                project=request.project,
                related_to=request.related_to,
                dry_run=request.dry_run,
                confidence_threshold=request.confidence_threshold,
            )
            try:
                response = await _auto_review_reflection_candidate(
                    request=candidate_request,
                    http_request=http_request,
                    org=org,
                    ctx=ctx,
                    accessible_projects=accessible_projects,
                )
                archived = False
                review_state = response.review_state
                if (
                    request.archive_exceptions
                    and not request.dry_run
                    and _should_archive_reflection_exception(
                        response,
                        archive_reasons=archive_reasons,
                    )
                ):
                    archived_memory = await _archive_reflection_exception_candidate(
                        response=response,
                        org=org,
                        ctx=ctx,
                        request=http_request,
                        project_id=request.project,
                    )
                    archived = archived_memory is not None
                    if archived_memory is not None:
                        review_state = archived_memory.review_state
                results.append(
                    serialization.drain_item_from_autonomy(
                        response,
                        archived=archived,
                        review_state=review_state,
                    )
                )
            except HTTPException as exc:
                results.append(
                    serialization.drain_error_item(
                        candidate.id, error=exc.detail, dry_run=request.dry_run
                    )
                )
            except Exception as exc:
                log.warning(
                    "drain_reflection_review_candidate_failed",
                    candidate_id=candidate.id,
                    error=str(exc),
                    exc_info=True,
                )
                results.append(
                    serialization.drain_error_item(
                        candidate.id, error=str(exc), dry_run=request.dry_run
                    )
                )
        return serialization.drain_response(request=request, results=results)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("drain_reflection_review_failed", error=str(exc))
        raise HTTPException(
            status_code=500,
            detail="Failed to drain reflection review queue.",
        ) from exc
