"""Agent context pack endpoints."""

import asyncio
import time
from typing import Any, cast

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
    SearchResult,
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
from sibyl_core.retrieval.fusion import rrf_merge_with_metadata
from sibyl_core.retrieval.refinement import (
    MAX_FEEDBACK_DOCUMENTS,
    RetrievalFeedbackDocument,
    normalize_retrieval_question,
    plan_deterministic_refinement_queries,
)

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
_DETERMINISTIC_REFINEMENT_USAGE: dict[str, str | int | float | bool | None] = {
    "provider": "deterministic",
    "model": "pseudo_relevance_feedback_v3",
    "requests": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "cost_usd": 0.0,
    "cost_complete": True,
}


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
) -> SearchRequest:
    assert request.evidence is not None
    return SearchRequest(
        query=query,
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


def _result_key(result: SearchResult) -> str:
    return f"{result.result_origin}:{result.id}"


def _feedback_source_id(result: SearchResult) -> str:
    value = result.metadata.get("operational_source_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _result_key(result)


def _refinement_frontier(
    responses: list[SearchResponse],
    *,
    limit: int = MAX_FEEDBACK_DOCUMENTS,
) -> list[SearchResult]:
    fused = rrf_merge_with_metadata(
        [[(result, result.score) for result in response.results] for response in responses],
        dedup_key=_result_key,
        limit=limit,
    )
    return [result for result, _score, _metadata in fused]


def _fuse_context_evidence(
    *,
    question: str,
    query_specs: list[dict[str, Any]],
    planned_queries: list[dict[str, Any]],
    responses: list[SearchResponse],
    limit: int,
    failures: list[dict[str, str | int]],
    planner_usage: dict[str, str | int | float | bool | None],
    planner_status: str = "success",
    refinement_rounds: int = 0,
    refinement_novel_result_counts: list[int] | None = None,
    refinement_stop_reason: str | None = None,
) -> SearchResponse:
    result_lists = [
        [(result, result.score) for result in response.results] for response in responses
    ]
    fused = rrf_merge_with_metadata(
        result_lists,
        list_names=[spec["name"] for spec in query_specs],
        dedup_key=_result_key,
    )
    fused_by_key = {
        _result_key(result): (result, score, metadata) for result, score, metadata in fused
    }

    ordered_keys: list[str] = []
    seen: set[str] = set()

    def reserve_unique(response: SearchResponse, count: int) -> int:
        reserved = 0
        for result in response.results:
            key = _result_key(result)
            if key not in seen:
                ordered_keys.append(key)
                seen.add(key)
                reserved += 1
                if reserved >= count:
                    break
        return reserved

    original_reservation_target = max(1, (limit + 1) // 2)
    original_reserved_count = reserve_unique(responses[0], original_reservation_target)
    supplemental_reserved_count = 0
    for response in responses[1:]:
        if len(ordered_keys) >= limit:
            break
        supplemental_reserved_count += reserve_unique(response, 1)
    for result, _score, _metadata in fused:
        key = _result_key(result)
        if key not in seen:
            ordered_keys.append(key)
            seen.add(key)

    selected: list[SearchResult] = []
    for key in ordered_keys[:limit]:
        result, score, fusion_metadata = fused_by_key[key]
        selected.append(
            result.model_copy(
                update={
                    "score": score,
                    "metadata": {
                        **result.metadata,
                        "retrieval_fusion": fusion_metadata,
                    },
                }
            )
        )

    unique_count = len(fused)
    return SearchResponse(
        results=selected,
        total=unique_count,
        query=question,
        filters={
            "retrieval_mode": "accurate",
            "planner_status": planner_status,
            "planner_strategy": "deterministic_refinement_v3",
            "planner_usage": planner_usage,
            "planned_queries": planned_queries,
            "query_count": 1 + len(planned_queries),
            "successful_query_count": len(responses),
            "query_failures": failures,
            "refinement_rounds": refinement_rounds,
            "refinement_novel_result_counts": refinement_novel_result_counts or [],
            "refinement_stop_reason": refinement_stop_reason,
            "original_reservation_target": original_reservation_target,
            "original_reserved_count": original_reserved_count,
            "supplemental_reserved_count": supplemental_reserved_count,
            "query_filters": {
                spec["name"]: {
                    key: value
                    for key, value in response.filters.items()
                    if key != "embedding_usage"
                }
                for spec, response in zip(query_specs, responses, strict=True)
            },
        },
        graph_count=sum(result.result_origin == "graph" for result in selected),
        document_count=sum(result.result_origin == "document" for result in selected),
        raw_memory_count=sum(result.result_origin == "raw_memory" for result in selected),
        limit=limit,
        has_more=unique_count > limit,
    )


async def _execute_context_refinement_round(
    request: ContextPackRequest,
    *,
    round_specs: list[dict[str, Any]],
    org: AuthOrganization,
    ctx: AuthContext,
    embedding_usage: dict[str, str | int | float],
    seen_result_keys: set[str],
) -> tuple[
    list[tuple[dict[str, Any], SearchResponse]],
    list[dict[str, str | int]],
    list[SearchResult],
]:
    tasks = [
        asyncio.create_task(
            _execute_context_evidence_search(
                _context_evidence_request(request, query=spec["query"]),
                org=org,
                ctx=ctx,
                embedding_usage=embedding_usage,
            )
        )
        for spec in round_specs
    ]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    successful: list[tuple[dict[str, Any], SearchResponse]] = []
    failures: list[dict[str, str | int]] = []
    novel_results: list[SearchResult] = []
    for spec, outcome in zip(round_specs, outcomes, strict=True):
        if isinstance(outcome, Exception):
            failures.append(
                {
                    "query_index": int(spec["name"].removeprefix("supplemental_")),
                    "query": spec["query"],
                    "facet": spec["facet"],
                    "error_type": type(outcome).__name__,
                }
            )
            continue
        if isinstance(outcome, BaseException):
            raise outcome
        successful.append((spec, outcome))
        for result in outcome.results:
            key = _result_key(result)
            if key in seen_result_keys:
                continue
            seen_result_keys.add(key)
            novel_results.append(result)
    return successful, failures, novel_results


async def _execute_accurate_context_evidence_search(
    request: ContextPackRequest,
    *,
    org: AuthOrganization,
    ctx: AuthContext,
    embedding_usage: dict[str, str | int | float],
) -> SearchResponse:
    assert request.evidence is not None
    retrieval_goal = normalize_retrieval_question(request.goal)
    original_response = await _execute_context_evidence_search(
        _context_evidence_request(request, query=retrieval_goal),
        org=org,
        ctx=ctx,
        embedding_usage=embedding_usage,
    )

    responses = [original_response]
    query_specs: list[dict[str, Any]] = [
        {"name": "original", "query": retrieval_goal, "facet": "original", "round": 0}
    ]
    planned_queries: list[dict[str, Any]] = []
    failures: list[dict[str, str | int]] = []
    seen_queries = [retrieval_goal]
    seen_result_keys = {_result_key(result) for result in original_response.results}
    frontier = list(original_response.results)
    remaining_queries = request.evidence.max_planned_queries
    refinement_novel_result_counts: list[int] = []
    refinement_stop_reason: str | None = None
    planner_error_type: str | None = None

    for round_index in range(1, 3):
        if remaining_queries <= 0:
            refinement_stop_reason = "query_budget_exhausted"
            break
        round_query_limit = min(1 if round_index == 1 else 2, remaining_queries)
        try:
            round_plan = plan_deterministic_refinement_queries(
                retrieval_goal,
                [
                    RetrievalFeedbackDocument(
                        id=_result_key(result),
                        text=f"{result.name}\n{result.content}",
                        source_id=_feedback_source_id(result),
                        raw_observation_projection=(
                            result.metadata.get("projection_kind") == "raw_observation"
                        ),
                        evidence_content_type=(
                            str(content_type)
                            if (content_type := result.metadata.get("evidence_content_type"))
                            else None
                        ),
                        projection_kind=(
                            str(projection_kind)
                            if (projection_kind := result.metadata.get("projection_kind"))
                            else None
                        ),
                    )
                    for result in frontier
                ],
                max_queries=round_query_limit,
                seen_queries=seen_queries,
            )
        except Exception as exc:
            planner_error_type = type(exc).__name__
            refinement_stop_reason = "planner_error"
            break
        if not round_plan:
            refinement_stop_reason = "no_refinement_terms"
            break

        round_specs: list[dict[str, Any]] = []
        for item in round_plan:
            query_index = len(planned_queries) + 1
            spec = {
                "name": f"supplemental_{query_index}",
                "query": item.query,
                "facet": item.facet,
                "round": round_index,
                "source_result_ids": list(item.source_result_ids),
                "added_terms": list(item.added_terms),
            }
            planned_queries.append(spec)
            round_specs.append(spec)
            seen_queries.append(item.query)
        successful, round_failures, novel_results = await _execute_context_refinement_round(
            request,
            round_specs=round_specs,
            org=org,
            ctx=ctx,
            embedding_usage=embedding_usage,
            seen_result_keys=seen_result_keys,
        )
        failures.extend(round_failures)
        for spec, response in successful:
            responses.append(response)
            query_specs.append(spec)

        remaining_queries -= len(round_specs)
        refinement_novel_result_counts.append(len(novel_results))
        if not successful:
            refinement_stop_reason = "all_queries_failed"
            break
        if not novel_results:
            refinement_stop_reason = "no_new_results"
            break
        frontier = _refinement_frontier(responses)

    if refinement_stop_reason is None:
        refinement_stop_reason = (
            "query_budget_exhausted" if remaining_queries <= 0 else "round_limit_reached"
        )

    planner_status = "success"
    if planner_error_type is not None:
        planner_status = "partial" if len(responses) > 1 else "fallback"
    fused = _fuse_context_evidence(
        question=request.goal,
        query_specs=query_specs,
        planned_queries=planned_queries,
        responses=responses,
        limit=request.evidence.limit,
        failures=failures,
        planner_usage=dict(_DETERMINISTIC_REFINEMENT_USAGE),
        planner_status=planner_status,
        refinement_rounds=len(refinement_novel_result_counts),
        refinement_novel_result_counts=refinement_novel_result_counts,
        refinement_stop_reason=refinement_stop_reason,
    )
    if planner_error_type is not None:
        fused.filters["planner_error_type"] = planner_error_type
    return fused


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
        retrieval_goal = normalize_retrieval_question(request.goal)

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
                allowed_memory_scope_keys=set(ctx.api_key_memory_scope_keys)
                if ctx.api_key_memory_scope_keys is not None
                else None,
            )

        evidence_response = None
        if request.evidence is None:
            pack = await compile_pack()
        else:
            try:
                embedding_provider = configured_embedding_provider()
            except ValueError as exc:
                raise RuntimeError("context evidence embedding configuration failed") from exc
            with capture_embedding_usage(embedding_provider) as embedding_usage:
                pack_task = asyncio.create_task(compile_pack())
                if request.evidence.retrieval_mode == "accurate":
                    evidence_task = asyncio.create_task(
                        _execute_accurate_context_evidence_search(
                            request,
                            org=org,
                            ctx=ctx,
                            embedding_usage=embedding_usage,
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
                try:
                    pack, evidence_response = await asyncio.gather(pack_task, evidence_task)
                except BaseException:
                    pack_task.cancel()
                    evidence_task.cancel()
                    await asyncio.gather(pack_task, evidence_task, return_exceptions=True)
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
