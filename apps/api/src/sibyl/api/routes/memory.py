"""Raw memory API routes."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import (
    RawMemoryRecallRequest,
    RawMemoryRecallResponse,
    RawMemoryRememberRequest,
    RawMemoryResponse,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.db.models import Organization, OrganizationRole, ProjectRole
from sibyl.persistence.auth_runtime import get_project_record_by_graph_id
from sibyl_core.services.surreal_content import (
    AGENT_DIARY_CAPTURE_SURFACE,
    RawMemory,
    recall_raw_memory,
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

router = APIRouter(prefix="/memory", tags=["memory"])


def _is_org_admin(ctx: AuthContext) -> bool:
    role = getattr(ctx.org_role, "value", ctx.org_role)
    return str(role) in {OrganizationRole.OWNER.value, OrganizationRole.ADMIN.value}


async def _authorize_scope(
    *,
    org: Organization,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
    required_project_role: ProjectRole,
) -> None:
    if memory_scope == "project":
        if not scope_key:
            return
        await get_project_record_by_graph_id(
            organization_id=org.id,
            graph_project_id=scope_key,
        )
        await verify_entity_project_access(
            None,
            ctx,
            scope_key,
            required_role=required_project_role,
        )
        return

    if memory_scope in {"delegated", "team", "shared"} and not _is_org_admin(ctx):
        raise HTTPException(
            status_code=403,
            detail=f"{memory_scope} raw memory requires owner/admin access until scope ACLs exist",
        )


async def _authorize_project_filter(
    *,
    org: Organization,
    ctx: AuthContext,
    project_id: str | None,
    required_project_role: ProjectRole,
) -> None:
    if not project_id:
        return
    await get_project_record_by_graph_id(
        organization_id=org.id,
        graph_project_id=project_id,
    )
    await verify_entity_project_access(
        None,
        ctx,
        project_id,
        required_role=required_project_role,
    )


def _diary_metadata(
    *,
    metadata: dict[str, object],
    diary: bool,
    agent_id: str | None,
    project_id: str | None,
) -> dict[str, object]:
    if not diary:
        return dict(metadata)
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for diary memory")
    out = dict(metadata)
    out["agent_id"] = agent_id
    out["memory_kind"] = "agent_diary"
    if project_id:
        out["project_id"] = project_id
    return out


def _validate_diary_request(*, diary: bool, agent_id: str | None, memory_scope: str) -> None:
    if not diary:
        return
    if memory_scope != "private":
        raise HTTPException(status_code=400, detail="diary memory must use private scope")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for diary memory")


def _raw_memory_response(memory: RawMemory) -> RawMemoryResponse:
    return RawMemoryResponse(
        id=memory.id,
        organization_id=memory.organization_id,
        source_id=memory.source_id,
        principal_id=memory.principal_id,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        title=memory.title,
        raw_content=memory.raw_content,
        tags=memory.tags,
        metadata=memory.metadata,
        provenance=memory.provenance,
        capture_surface=memory.capture_surface,
        captured_at=memory.captured_at,
        created_at=memory.created_at,
        score=memory.score,
    )


@router.post(
    "/raw",
    response_model=RawMemoryResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def remember_raw(
    request: RawMemoryRememberRequest,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> RawMemoryResponse:
    """Store verbatim memory before extraction or graph reflection."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        capture_surface = (
            AGENT_DIARY_CAPTURE_SURFACE if request.diary else request.capture_surface
        )
        source_id = request.source_id or f"{capture_surface}:manual"
        _validate_diary_request(
            diary=request.diary,
            agent_id=request.agent_id,
            memory_scope=request.memory_scope,
        )
        await _authorize_project_filter(
            org=org,
            ctx=ctx,
            project_id=request.project_id,
            required_project_role=ProjectRole.CONTRIBUTOR,
        )
        await _authorize_scope(
            org=org,
            ctx=ctx,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            required_project_role=ProjectRole.CONTRIBUTOR,
        )
        metadata = _diary_metadata(
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
        return _raw_memory_response(memory)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        log.exception("remember_raw_memory_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to remember raw memory.") from e


@router.post(
    "/raw/recall",
    response_model=RawMemoryRecallResponse,
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)
async def recall_raw(
    request: RawMemoryRecallRequest,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> RawMemoryRecallResponse:
    """Recall verbatim memories through scoped retrieval."""
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        _validate_diary_request(
            diary=request.diary,
            agent_id=request.agent_id,
            memory_scope=request.memory_scope,
        )
        await _authorize_scope(
            org=org,
            ctx=ctx,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            required_project_role=ProjectRole.VIEWER,
        )
        await _authorize_project_filter(
            org=org,
            ctx=ctx,
            project_id=request.project_id,
            required_project_role=ProjectRole.VIEWER,
        )
        memories = await recall_raw_memory(
            organization_id=str(org.id),
            principal_id=principal_id,
            query=request.query,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            agent_id=request.agent_id,
            project_id=request.project_id,
            limit=request.limit,
        )
        return RawMemoryRecallResponse(
            query=request.query,
            limit=request.limit,
            memories=[_raw_memory_response(memory) for memory in memories],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        log.exception("recall_raw_memory_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to recall raw memory.") from e
