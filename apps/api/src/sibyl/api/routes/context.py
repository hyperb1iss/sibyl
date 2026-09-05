"""Agent context pack endpoints."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, Literal, cast

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
from sibyl_core.ai.operational_distillation import OPERATIONAL_NOTE_CATEGORY
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from sibyl_core.embeddings.providers import capture_embedding_usage, configured_embedding_provider
from sibyl_core.models.context import ContextPack
from sibyl_core.observability import elapsed_ms, telemetry_registry
from sibyl_core.retrieval.operational_evidence import compose_operational_evidence
from sibyl_core.retrieval.refinement import normalize_retrieval_question

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


def _context_evidence_request(
    request: ContextPackRequest,
    *,
    query: str,
    candidate_limit: int | None = None,
) -> SearchRequest:
    assert request.evidence is not None
    return SearchRequest(
        query=query,
        types=request.evidence.types,
        project=request.project,
        limit=candidate_limit or request.evidence.limit,
        include_content=True,
        content_max_chars=request.evidence.content_max_chars,
        include_documents=False,
        include_graph=True,
        include_raw_memory=True,
        use_enhanced=True,
        boost_recent=False,
        include_retrieval_diagnostics=request.evidence.include_retrieval_diagnostics,
        record_exposure=(request.record_exposure and not request.evidence.reserve_distilled_notes),
        knn_type_overfetch=request.evidence.knn_type_overfetch,
    )


def _naive_retrieval_selected(request: ContextPackRequest) -> bool:
    """Whether this request selected the 1.3 Phase 0 control arm."""

    return request.evidence is not None and request.evidence.retrieval_mode == "naive"


def _reject_machine_knobs_under_the_arm(request: ContextPackRequest) -> None:
    """Refuse settings the arm cannot honour, rather than half-applying them.

    `knn_type_overfetch` tunes how deep the machine's typed vector read walks
    around the HNSW bracket. The arm has no typed overfetch stage, and the pack
    plan and the evidence plan would read the field from different places, so a
    request setting it got it applied to one half of its own pack and dropped
    from the other. A knob that lands on half a request is worse than a knob
    that is refused: the run looks configured and is not.
    """

    if not _naive_retrieval_selected(request):
        return
    assert request.evidence is not None
    requested = {
        "knn_type_overfetch": request.knn_type_overfetch,
        "evidence.knn_type_overfetch": request.evidence.knn_type_overfetch,
    }
    composition_fields = {
        "operational_note_dedupe_mode",
        "operational_note_lane_mode",
    }
    requested.update(
        {
            f"evidence.{field}": getattr(request.evidence, field)
            for field in composition_fields & request.evidence.model_fields_set
        }
    )
    offenders = sorted(name for name, value in requested.items() if value)
    if offenders:
        raise HTTPException(
            status_code=400,
            detail=(
                f"retrieval_mode=naive cannot honour {', '.join(offenders)}: "
                "the control arm runs neither typed evidence composition nor typed overfetch"
            ),
        )


async def _execute_naive_context_evidence_search(
    request: ContextPackRequest,
    *,
    query: str,
    org: AuthOrganization,
    ctx: AuthContext,
    accessible_projects: set[str] | None,
    embedding_provider: Any,
) -> SearchResponse:
    """Serve evidence from the naive-strong arm instead of the enhanced pipeline.

    The plan is built by the same constructor the machine uses, so scope
    filtering, API-key memory grants, and project authorization are identical
    across arms and only the retrieval lanes differ. That is the property the
    race depends on: a control arm that also relaxed authorization would be
    reading a different corpus rather than running a simpler pipeline.
    """

    assert request.evidence is not None
    from sibyl_core.models.context import ContextFacet
    from sibyl_core.retrieval.naive import naive_search
    from sibyl_core.retrieval.search import build_context_retrieval_plan

    plan = build_context_retrieval_plan(
        query=query,
        organization_id=str(org.id),
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: list(request.evidence.types)},
        principal_id=ctx.user_id,
        project=request.project,
        accessible_projects=accessible_projects,
        agent_id=request.agent_id,
        limit=request.evidence.limit,
        allowed_memory_scope_keys=(
            set(ctx.api_key_memory_scope_keys)
            if ctx.api_key_memory_scope_keys is not None
            else None
        ),
    )
    try:
        result = await naive_search(
            plan=plan,
            types=request.evidence.types,
            limit=request.evidence.limit,
            include_content=True,
            embedding_provider=embedding_provider,
            char_budget=request.evidence.char_budget,
            content_max_chars=request.evidence.content_max_chars,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise RuntimeError("naive context evidence retrieval failed") from exc
    # The arm returns the core retrieval dataclass; the route contract is the
    # API schema, converted the same way the enhanced path converts it.
    return SearchResponse(**asdict(result))


def _distilled_context_evidence_request(
    request: ContextPackRequest,
    *,
    query: str,
) -> SearchRequest:
    assert request.evidence is not None
    return SearchRequest(
        query=query,
        types=["note"],
        category=OPERATIONAL_NOTE_CATEGORY,
        project=request.project,
        limit=request.evidence.limit,
        include_content=True,
        content_max_chars=request.evidence.content_max_chars,
        include_documents=False,
        include_graph=True,
        include_raw_memory=False,
        use_enhanced=True,
        boost_recent=False,
        include_retrieval_diagnostics=request.evidence.include_retrieval_diagnostics,
        record_exposure=False,
        knn_type_overfetch=request.evidence.knn_type_overfetch,
    )


async def _execute_distilled_context_evidence_search(
    request: ContextPackRequest,
    *,
    query: str,
    org: AuthOrganization,
    ctx: AuthContext,
    embedding_usage: dict[str, str | int | float],
) -> tuple[SearchResponse | None, str | None]:
    try:
        response = await _execute_context_evidence_search(
            _distilled_context_evidence_request(request, query=query),
            org=org,
            ctx=ctx,
            embedding_usage=embedding_usage,
        )
    except Exception as exc:
        log.warning(
            "context_distilled_evidence_unavailable",
            error_type=type(exc).__name__,
        )
        return None, type(exc).__name__
    return response, None


def _compose_context_evidence_response(
    raw_response: SearchResponse,
    typed_response: SearchResponse | None,
    *,
    limit: int,
    typed_error: str | None,
    char_budget: int | None = None,
    operational_note_dedupe_mode: Literal["source", "source_kind"] = "source",
    operational_note_lane_mode: Literal["reserved", "additive"] = "reserved",
    include_activity_receipt: bool = False,
) -> SearchResponse:
    typed_results = typed_response.results if typed_response is not None else []
    selected, receipt = compose_operational_evidence(
        typed_results=typed_results,
        raw_results=raw_response.results,
        limit=limit,
        char_budget=char_budget,
        operational_note_dedupe_mode=operational_note_dedupe_mode,
        operational_note_lane_mode=operational_note_lane_mode,
        include_activity_receipt=include_activity_receipt,
    )
    receipt.update(
        {
            "typed_search_status": "degraded" if typed_error else "success",
            "typed_search_error_type": typed_error,
            "typed_query_filters": typed_response.filters if typed_response is not None else {},
        }
    )
    total = raw_response.total + int(receipt["typed_candidate_count"])
    return SearchResponse(
        results=selected,
        total=total,
        query=raw_response.query,
        filters={
            **raw_response.filters,
            "evidence_composition": receipt,
        },
        graph_count=sum(result.result_origin == "graph" for result in selected),
        document_count=sum(result.result_origin == "document" for result in selected),
        raw_memory_count=sum(result.result_origin == "raw_memory" for result in selected),
        limit=limit,
        offset=raw_response.offset,
        has_more=(
            raw_response.has_more
            or bool(typed_response and typed_response.has_more)
            or total > len(selected)
        ),
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
    principal_id: str | None,
    accessible_projects: set[str] | None,
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
            principal_id=principal_id,
            accessible_projects=accessible_projects,
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


async def _compile_context_with_evidence(
    request: ContextPackRequest,
    *,
    retrieval_goal: str,
    org: AuthOrganization,
    ctx: AuthContext,
    accessible_projects: set[str] | None,
    compile_pack: Callable[[], Awaitable[ContextPack]],
) -> tuple[ContextPack, SearchResponse]:
    assert request.evidence is not None
    try:
        embedding_provider = configured_embedding_provider()
    except ValueError as exc:
        raise RuntimeError("context evidence embedding configuration failed") from exc

    typed_outcome: tuple[SearchResponse | None, str | None] = (None, None)
    with capture_embedding_usage(embedding_provider) as embedding_usage:
        pack_task = asyncio.create_task(compile_pack())
        if request.evidence.retrieval_mode == "naive":
            evidence_task = asyncio.create_task(
                _execute_naive_context_evidence_search(
                    request,
                    query=retrieval_goal,
                    org=org,
                    ctx=ctx,
                    accessible_projects=accessible_projects,
                    embedding_provider=embedding_provider,
                )
            )
        else:
            evidence_task = asyncio.create_task(
                _execute_context_evidence_search(
                    _context_evidence_request(request, query=retrieval_goal),
                    org=org,
                    ctx=ctx,
                    embedding_usage=embedding_usage,
                )
            )
        # The reserved distilled-notes lane is a second typed retrieval plus a
        # composition step, so it belongs to the machine the arm is measured
        # against. Under the arm it stays off whatever the request asked for,
        # rather than quietly reintroducing the surface being tested.
        typed_task = (
            asyncio.create_task(
                _execute_distilled_context_evidence_search(
                    request,
                    query=retrieval_goal,
                    org=org,
                    ctx=ctx,
                    embedding_usage=embedding_usage,
                )
            )
            if request.evidence.reserve_distilled_notes
            and request.evidence.retrieval_mode != "naive"
            else None
        )
        try:
            if typed_task is None:
                pack, evidence_response = await asyncio.gather(pack_task, evidence_task)
            else:
                pack, evidence_response, typed_outcome = await asyncio.gather(
                    pack_task,
                    evidence_task,
                    typed_task,
                )
        except BaseException:
            pending_tasks = [pack_task, evidence_task]
            if typed_task is not None:
                pending_tasks.append(typed_task)
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)
            raise

    if request.evidence.retrieval_mode == "fast":
        evidence_response.filters.update(
            {
                "retrieval_mode": "fast",
                "planner_status": "not_requested",
                "planned_queries": [],
                "query_count": 1,
            }
        )
    if request.evidence.retrieval_mode == "naive":
        # The arm runs one search and no planner, so it reports the same
        # planner receipt fast does. Consumers that key off planner_status stay
        # correct without learning a third shape.
        evidence_response.filters.update(
            {
                "retrieval_mode": "naive",
                "retrieval_arm": "naive",
                "planner_status": "not_requested",
                "planned_queries": [],
                "query_count": 1,
                "reserve_distilled_notes": False,
            }
        )
    if request.evidence.retrieval_mode == "naive" and request.record_exposure:
        # The enhanced path stamps exposure inside the search it runs, which the
        # arm does not use. Stamped here instead, so a run under the arm leaves
        # the same receipts and exposure analysis can compare the two arms
        # rather than reading the arm as a run that surfaced nothing.
        from sibyl_core.tools.usage_exposure import annotate_search_result_exposures

        evidence_response.filters["usage_exposure"] = await annotate_search_result_exposures(
            evidence_response.results,
            organization_id=str(org.id),
            principal_id=ctx.user_id,
            project_id=request.project,
            source_surface="context_pack_evidence",
            request_metadata={"agent_id": request.agent_id} if request.agent_id else None,
        )
    if typed_task is not None:
        typed_response, typed_error = typed_outcome
        evidence_response = _compose_context_evidence_response(
            evidence_response,
            typed_response,
            limit=request.evidence.limit,
            typed_error=typed_error,
            char_budget=request.evidence.char_budget,
            operational_note_dedupe_mode=request.evidence.operational_note_dedupe_mode,
            operational_note_lane_mode=request.evidence.operational_note_lane_mode,
            include_activity_receipt=bool(
                {
                    "operational_note_dedupe_mode",
                    "operational_note_lane_mode",
                }
                & request.evidence.model_fields_set
            ),
        )
        if request.record_exposure:
            from sibyl_core.tools.usage_exposure import annotate_search_result_exposures

            exposure_summary = await annotate_search_result_exposures(
                evidence_response.results,
                organization_id=str(org.id),
                principal_id=ctx.user_id,
                project_id=request.project,
                source_surface="context_pack_evidence",
                request_metadata={"agent_id": request.agent_id} if request.agent_id else None,
            )
            evidence_response.filters["usage_exposure"] = exposure_summary
    evidence_response.filters["embedding_usage"] = dict(embedding_usage)
    return pack, evidence_response


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
            render_context_pack,
        )

        accessible_projects = await _resolve_accessible_context_projects(
            ctx=ctx,
            project=request.project,
        )
        retrieval_goal = normalize_retrieval_question(request.goal)
        _reject_machine_knobs_under_the_arm(request)

        async def compile_pack() -> ContextPack:
            return await compile_context(
                goal=request.goal,
                retrieval_query=retrieval_goal,
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
                knn_type_overfetch=request.knn_type_overfetch,
                naive_retrieval=_naive_retrieval_selected(request),
                allowed_memory_scope_keys=set(ctx.api_key_memory_scope_keys)
                if ctx.api_key_memory_scope_keys is not None
                else None,
                include_documents=request.evidence is None,
            )

        evidence_response = None
        if request.evidence is None:
            pack = await compile_pack()
        else:
            pack, evidence_response = await _compile_context_with_evidence(
                request,
                retrieval_goal=retrieval_goal,
                org=org,
                ctx=ctx,
                accessible_projects=accessible_projects,
                compile_pack=compile_pack,
            )
        payload = context_pack_to_dict(pack)
        rendered = render_context_pack(
            pack,
            token_budget=request.markdown_token_budget,
            request_id=getattr(getattr(http_request, "state", None), "request_id", None),
        )
        payload["markdown"] = rendered.markdown
        payload["render_receipt"] = rendered.receipt
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
            principal_id=getattr(ctx, "user_id", None),
            accessible_projects=accessible_projects,
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
