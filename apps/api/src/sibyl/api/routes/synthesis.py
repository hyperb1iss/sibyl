"""Source-grounded synthesis endpoints."""

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import (
    SynthesisDraftRequest,
    SynthesisDraftResponse,
    SynthesisPlanRequest,
    SynthesisPlanResponse,
)
from sibyl.auth.authorization import ProjectAuthorizationError, verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from sibyl_core.auth.memory_policy import authorize_memory_write
from sibyl_core.models.synthesis import SynthesisRequest, SynthesisRun, SynthesisSectionRequest
from sibyl_core.services import synthesis as synthesis_service

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

router = APIRouter(
    prefix="/synthesis",
    tags=["synthesis"],
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)


async def _resolve_accessible_synthesis_projects(
    *,
    ctx: AuthContext,
    project: str | None,
) -> set[str] | None:
    if project:
        await verify_entity_project_access(
            None,
            ctx,
            project,
            required_role=ProjectRole.VIEWER,
        )
        return {str(project)}
    accessible_projects = await list_accessible_project_graph_ids(ctx)
    return {str(project_id) for project_id in accessible_projects or set()}


def _core_synthesis_request(request: SynthesisPlanRequest) -> SynthesisRequest:
    return SynthesisRequest(
        goal=request.goal,
        output_type=request.output_type,
        audience=request.audience,
        depth=request.depth,
        seed_query=request.seed_query,
        project=request.project,
        domain=request.domain,
        entity_ids=list(request.entity_ids),
        decision_ids=list(request.decision_ids),
        task_ids=list(request.task_ids),
        artifact_ids=list(request.artifact_ids),
        required_sections=[
            SynthesisSectionRequest(
                title=section.title,
                prompt=section.prompt,
                required_source_ids=list(section.required_source_ids),
            )
            for section in request.required_sections
        ],
        constraints=list(request.constraints),
        max_sections=request.max_sections,
        include_neighborhoods=request.include_neighborhoods,
    )


async def _planned_materialized_run(
    request: SynthesisPlanRequest,
    *,
    org: AuthOrganization,
    ctx: AuthContext,
) -> tuple[SynthesisRun, set[str] | None]:
    accessible_projects = await _resolve_accessible_synthesis_projects(
        ctx=ctx,
        project=request.project,
    )
    core_request = _core_synthesis_request(request)
    run = await synthesis_service.plan_synthesis(
        core_request,
        organization_id=str(org.id),
        accessible_projects=accessible_projects,
        search_fn=synthesis_service.default_search,
        related_fn=synthesis_service.default_related_sources,
    )
    run = await synthesis_service.materialize_synthesis_section_packs(
        run,
        organization_id=str(org.id),
        principal_id=ctx.user_id,
        accessible_projects=accessible_projects,
        context_fn=synthesis_service.default_context_pack,
    )
    return run, accessible_projects


async def _authorize_synthesis_artifact_write(
    *,
    request: SynthesisDraftRequest,
    ctx: AuthContext,
) -> str | None:
    if getattr(ctx, "org_role", None) not in _WRITE_ROLES:
        raise HTTPException(status_code=403, detail="insufficient_org_role")
    scope_key = request.scope_key
    accessible_projects: set[str] | None = None
    if request.memory_scope == "project":
        scope_key = scope_key or request.project
        if not scope_key:
            raise HTTPException(status_code=400, detail="missing_scope_key")
        await verify_entity_project_access(
            None,
            ctx,
            scope_key,
            required_role=ProjectRole.CONTRIBUTOR,
        )
        accessible_projects = {scope_key}
    decision = authorize_memory_write(
        principal_id=ctx.user_id,
        memory_scope=request.memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
    )
    if not decision.allowed:
        status_code = 400 if decision.reason == "missing_scope_key" else 403
        raise HTTPException(status_code=status_code, detail=decision.reason)
    return scope_key


@router.post("/plan", response_model=SynthesisPlanResponse)
async def plan_synthesis_route(
    request: SynthesisPlanRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SynthesisPlanResponse:
    """Create a deterministic source-aware synthesis outline."""
    try:
        run, _ = await _planned_materialized_run(request, org=org, ctx=ctx)
        return SynthesisPlanResponse.model_validate(synthesis_service.synthesis_run_to_dict(run))
    except (ProjectAccessDeniedError, ProjectAuthorizationError):
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("synthesis_plan_failed", goal=request.goal, error=str(exc))
        raise HTTPException(
            status_code=500,
            detail="Synthesis planning failed. Please try again.",
        ) from exc


@router.post("/draft", response_model=SynthesisDraftResponse)
async def draft_synthesis_route(
    request: SynthesisDraftRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SynthesisDraftResponse:
    """Draft and verify a source-grounded synthesis artifact."""
    try:
        run, _ = await _planned_materialized_run(request, org=org, ctx=ctx)
        artifact = synthesis_service.draft_synthesis_artifact(
            run,
            output_format=request.output_format,
        )
        run = synthesis_service.apply_synthesis_verification(run)
        if request.remember:
            scope_key = await _authorize_synthesis_artifact_write(
                request=request,
                ctx=ctx,
            )
            artifact = await synthesis_service.remember_synthesis_artifact(
                artifact,
                run,
                organization_id=str(org.id),
                principal_id=ctx.user_id,
                memory_scope=request.memory_scope,
                scope_key=scope_key,
                tags=request.tags,
                remember_fn=synthesis_service.default_remember_artifact,
            )
        payload = synthesis_service.synthesis_run_to_dict(run)
        payload["artifact"] = synthesis_service.synthesis_artifact_to_dict(artifact)
        return SynthesisDraftResponse.model_validate(payload)
    except (ProjectAccessDeniedError, ProjectAuthorizationError):
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("synthesis_draft_failed", goal=request.goal, error=str(exc))
        raise HTTPException(
            status_code=500,
            detail="Synthesis drafting failed. Please try again.",
        ) from exc
