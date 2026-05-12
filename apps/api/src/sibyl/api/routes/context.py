"""Agent context pack endpoints."""

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import (
    ContextPackRequest,
    ContextPackResponse,
    ReflectionRequest,
    ReflectionResponse,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole

log = structlog.get_logger()
_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)

router = APIRouter(
    prefix="/context",
    tags=["context"],
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)


def _append_unique_ids(existing: list[str] | None, additions: list[str] | None) -> list[str] | None:
    links = list(existing or [])
    seen = set(links)
    for item in additions or []:
        if item not in seen:
            links.append(item)
            seen.add(item)
    return links or None


async def _resolve_accessible_context_projects(
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


async def _resolve_reflection_links(
    *,
    org_id: str,
    project: str | None,
    related_to: list[str] | None,
    task_ids: list[str] | None,
    active_task: bool,
) -> list[str] | None:
    links = _append_unique_ids(related_to, task_ids)
    if not active_task or not project:
        return links

    from sibyl_core.tools.core import explore

    try:
        response = await explore(
            mode="list",
            types=["task"],
            project=project,
            status="doing",
            limit=2,
            organization_id=org_id,
        )
    except Exception as exc:
        log.warning("reflect_active_task_lookup_failed", project=project, error=str(exc))
        return links

    entities = getattr(response, "entities", [])
    if len(entities) != 1:
        return links

    task_id = getattr(entities[0], "id", None)
    if not task_id:
        return links

    return _append_unique_ids(links, [str(task_id)])


@router.post("/pack", response_model=ContextPackResponse)
async def context_pack(
    request: ContextPackRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ContextPackResponse:
    """Compile a structured context pack for an agent goal."""
    try:
        from sibyl_core.tools.context import (
            compile_context,
            context_pack_to_dict,
            context_pack_to_markdown,
        )

        accessible_projects = await _resolve_accessible_context_projects(
            ctx=ctx,
            project=request.project,
        )

        pack = await compile_context(
            goal=request.goal,
            intent=request.intent,
            layer=request.layer,
            domain=request.domain,
            project=request.project,
            accessible_projects=accessible_projects,
            principal_id=ctx.user_id,
            agent_id=request.agent_id,
            organization_id=str(org.id),
            limit=request.limit,
            include_related=request.include_related,
            related_limit=request.related_limit,
        )
        payload = context_pack_to_dict(pack)
        payload["markdown"] = context_pack_to_markdown(pack)
        return ContextPackResponse.model_validate(payload)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("context_pack_failed", goal=request.goal, error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Context pack compilation failed. Please try again.",
        ) from e


@router.post("/reflect", response_model=ReflectionResponse)
async def reflect_context(
    request: ReflectionRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionResponse:
    """Reflect raw notes into durable memory candidates."""
    try:
        from sibyl_core.tools.core import (
            reflect_memory,
            reflection_pack_to_dict,
            reflection_pack_to_markdown,
        )

        await _resolve_accessible_context_projects(
            ctx=ctx,
            project=request.project,
        )
        related_to = await _resolve_reflection_links(
            org_id=str(org.id),
            project=request.project,
            related_to=request.related_to,
            task_ids=request.task_ids,
            active_task=request.active_task and request.persist,
        )

        pack = await reflect_memory(
            content=request.content,
            source_title=request.source_title,
            intent=request.intent.value,
            domain=request.domain,
            project=request.project,
            related_to=related_to,
            organization_id=str(org.id),
            persist=request.persist,
            persist_source=request.persist_source,
            limit=request.limit,
        )
        payload = reflection_pack_to_dict(pack)
        payload["markdown"] = reflection_pack_to_markdown(pack)
        return ReflectionResponse.model_validate(payload)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        log.exception("context_reflect_failed", source_title=request.source_title, error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Reflection failed. Please try again.",
        ) from e
