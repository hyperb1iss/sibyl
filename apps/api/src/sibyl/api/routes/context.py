"""Agent context pack endpoints."""

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import (
    ContextPackRequest,
    ContextPackResponse,
    ReflectionRequest,
    ReflectionResponse,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl.db.models import Organization, OrganizationRole
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids

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


@router.post("/pack", response_model=ContextPackResponse)
async def context_pack(
    request: ContextPackRequest,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ContextPackResponse:
    """Compile a structured context pack for an agent goal."""
    try:
        from sibyl_core.tools.context import (
            compile_context,
            context_pack_to_dict,
            context_pack_to_markdown,
        )

        accessible_projects = set(await list_accessible_project_graph_ids(ctx))
        if request.project and request.project not in accessible_projects:
            raise ProjectAccessDeniedError(
                project_id=request.project,
                required_role="viewer",
            )

        pack = await compile_context(
            goal=request.goal,
            intent=request.intent,
            domain=request.domain,
            project=request.project,
            accessible_projects=None if request.project else accessible_projects,
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
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionResponse:
    """Reflect raw notes into durable memory candidates."""
    try:
        from sibyl_core.tools.core import (
            reflect_memory,
            reflection_pack_to_dict,
            reflection_pack_to_markdown,
        )

        accessible_projects = set(await list_accessible_project_graph_ids(ctx))
        if request.project and request.project not in accessible_projects:
            raise ProjectAccessDeniedError(
                project_id=request.project,
                required_role="viewer",
            )

        pack = await reflect_memory(
            content=request.content,
            source_title=request.source_title,
            intent=request.intent.value,
            domain=request.domain,
            project=request.project,
            related_to=request.related_to,
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
