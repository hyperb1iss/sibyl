"""Agent context pack endpoints."""

import asyncio
import time
from typing import cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from sibyl.api.context_audit import (
    log_context_pack_audit,
    log_denied_render_audit,
    log_reflection_audit,
)
from sibyl.api.schemas import (
    ContextPackRequest,
    ContextPackResponse,
    ReflectionRequest,
    ReflectionResponse,
    SearchRequest,
    SearchResponse,
)
from sibyl.auth.authorization import ProjectAuthorizationError, verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from sibyl_core.embeddings.providers import capture_embedding_usage, configured_embedding_provider
from sibyl_core.models.context import ContextPack
from sibyl_core.observability import elapsed_ms, telemetry_registry

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
_REQUEST_AUTO_INJECT_SENTINEL: Request = cast("Request", None)


async def _execute_context_evidence_search(
    request: SearchRequest,
    *,
    org: AuthOrganization,
    ctx: AuthContext,
    embedding_usage: dict[str, str | int | float],
) -> SearchResponse:
    from sibyl.api.routes.search import execute_search_request

    try:
        return await execute_search_request(
            request,
            org=org,
            ctx=ctx,
            embedding_usage=embedding_usage,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise RuntimeError("context evidence retrieval failed") from exc


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
    required_project_role: ProjectRole = ProjectRole.VIEWER,
) -> set[str] | None:
    if project:
        await verify_entity_project_access(
            None,
            ctx,
            project,
            required_role=required_project_role,
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
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ContextPackResponse:
    """Compile a structured context pack for an agent goal."""
    started_at = time.perf_counter()
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

        async def compile_pack() -> ContextPack:
            return await compile_context(
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
                audit=request.audit,
                record_exposure=request.record_exposure,
                allowed_memory_scope_keys=set(ctx.api_key_memory_scope_keys)
                if ctx.api_key_memory_scope_keys is not None
                else None,
            )

        evidence_response = None
        if request.evidence is None:
            pack = await compile_pack()
        else:
            evidence_request = SearchRequest(
                query=request.goal,
                types=request.evidence.types,
                project=request.project,
                limit=request.evidence.limit,
                include_content=True,
                content_max_chars=request.evidence.content_max_chars,
                include_documents=False,
                include_graph=True,
                include_raw_memory=True,
                use_enhanced=True,
                boost_recent=False,
                include_retrieval_diagnostics=request.evidence.include_retrieval_diagnostics,
                record_exposure=request.record_exposure,
            )
            try:
                embedding_provider = configured_embedding_provider()
            except ValueError as exc:
                raise RuntimeError("context evidence embedding configuration failed") from exc
            with capture_embedding_usage(embedding_provider) as embedding_usage:
                pack_task = asyncio.create_task(compile_pack())
                evidence_task = asyncio.create_task(
                    _execute_context_evidence_search(
                        evidence_request,
                        org=org,
                        ctx=ctx,
                        embedding_usage=embedding_usage,
                    )
                )
                try:
                    pack, evidence_response = await asyncio.gather(pack_task, evidence_task)
                except BaseException:
                    pack_task.cancel()
                    evidence_task.cancel()
                    await asyncio.gather(pack_task, evidence_task, return_exceptions=True)
                    raise
            evidence_response.filters["embedding_usage"] = dict(embedding_usage)
        payload = context_pack_to_dict(pack)
        payload["markdown"] = context_pack_to_markdown(
            pack,
            token_budget=request.markdown_token_budget,
        )
        payload["evidence"] = evidence_response
        response = ContextPackResponse.model_validate(payload)
        await log_context_pack_audit(
            user_id=ctx.user_id,
            organization_id=str(org.id),
            request=http_request,
            pack=pack,
            project=request.project,
            accessible_projects=accessible_projects,
            source_surface="context_pack",
            agent_id=request.agent_id,
            limit=request.limit,
            include_related=request.include_related,
            related_limit=request.related_limit,
        )
        telemetry_registry().record_memory_operation(
            operation="context_pack",
            status="ok",
            duration_ms=elapsed_ms(started_at),
            result_count=response.total_items,
        )
        return response

    except (ProjectAccessDeniedError, ProjectAuthorizationError) as exc:
        telemetry_registry().record_memory_operation(
            operation="context_pack",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        await log_denied_render_audit(
            action="memory.context_pack.deny",
            user_id=ctx.user_id,
            organization_id=str(org.id),
            request=http_request,
            project=request.project,
            source_surface="context_pack",
            route_action="context_pack",
            reason=exc,
        )
        raise
    except HTTPException:
        telemetry_registry().record_memory_operation(
            operation="context_pack",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise
    except ValueError as e:
        telemetry_registry().record_memory_operation(
            operation="context_pack",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        telemetry_registry().record_memory_operation(
            operation="context_pack",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        log.exception("context_pack_failed", goal=request.goal, error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Context pack compilation failed. Please try again.",
        ) from e


@router.post("/reflect", response_model=ReflectionResponse)
async def reflect_context(
    request: ReflectionRequest,
    http_request: Request = _REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReflectionResponse:
    """Reflect raw notes into durable memory candidates."""
    started_at = time.perf_counter()
    try:
        from sibyl_core.tools.core import (
            reflect_memory,
            reflection_pack_to_dict,
            reflection_pack_to_markdown,
        )
        from sibyl_core.tools.usage_citation import record_cited_item_usages

        accessible_projects = await _resolve_accessible_context_projects(
            ctx=ctx,
            project=request.project,
            required_project_role=(
                ProjectRole.CONTRIBUTOR if request.persist else ProjectRole.VIEWER
            ),
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
            principal_id=getattr(ctx, "user_id", None),
            accessible_projects=accessible_projects,
            memory_scope="project" if request.project else "private",
            scope_key=request.project,
            persist=request.persist,
            persist_source=request.persist_source,
            persist_review=request.persist_review,
            limit=request.limit,
        )
        payload = reflection_pack_to_dict(pack)
        payload["markdown"] = reflection_pack_to_markdown(pack)
        if request.cited_ids:
            try:
                payload["citation_usage"] = await record_cited_item_usages(
                    request.cited_ids,
                    organization_id=str(org.id),
                    principal_id=getattr(ctx, "user_id", None),
                    project_id=request.project,
                    source_surface="context_reflect",
                    request_metadata={
                        "source_title": request.source_title,
                        "intent": request.intent.value,
                        "persist": request.persist,
                    },
                )
            except Exception as exc:
                log.warning(
                    "context_reflect_citation_usage_failed",
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
        response = ReflectionResponse.model_validate(payload)
        await log_reflection_audit(
            user_id=ctx.user_id,
            organization_id=str(org.id),
            request=http_request,
            pack=pack,
            project=request.project,
            accessible_projects=accessible_projects,
            source_surface="context_reflect",
            persist=request.persist,
            persist_source=request.persist_source,
            persist_review=request.persist_review,
            active_task=request.active_task,
            related_to=related_to,
            task_ids=request.task_ids,
            limit=request.limit,
        )
        telemetry_registry().record_memory_operation(
            operation="context_reflect",
            status="ok",
            duration_ms=elapsed_ms(started_at),
            result_count=response.total_candidates,
        )
        return response

    except (ProjectAccessDeniedError, ProjectAuthorizationError) as exc:
        telemetry_registry().record_memory_operation(
            operation="context_reflect",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        await log_denied_render_audit(
            action="memory.reflect.deny",
            user_id=ctx.user_id,
            organization_id=str(org.id),
            request=http_request,
            project=request.project,
            source_surface="context_reflect",
            route_action="context_reflect",
            reason=exc,
        )
        raise
    except HTTPException:
        telemetry_registry().record_memory_operation(
            operation="context_reflect",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise
    except ValueError as e:
        telemetry_registry().record_memory_operation(
            operation="context_reflect",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        telemetry_registry().record_memory_operation(
            operation="context_reflect",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        log.exception("context_reflect_failed", source_title=request.source_title, error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Reflection failed. Please try again.",
        ) from e
