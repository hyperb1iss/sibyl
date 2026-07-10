"""Unified search, explore, and temporal query endpoints.

Search endpoint searches both knowledge graph AND crawled documentation,
merging results by relevance score. Temporal queries expose bi-temporal
edge metadata for point-in-time queries and conflict detection.
"""

import time
from dataclasses import asdict

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.schemas import (
    ExploreRequest,
    ExploreResponse,
    SearchRequest,
    SearchResponse,
    TemporalEdgeSchema,
    TemporalRequest,
    TemporalResponse,
)
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl_core.auth import (
    AuthOrganization,
    MemoryPolicyContext,
    OrganizationRole,
    ProjectRole,
    authorize_memory_read,
)
from sibyl_core.observability import elapsed_ms, telemetry_registry

log = structlog.get_logger()
_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)

router = APIRouter(
    prefix="/search",
    tags=["search"],
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)


def _policy_http_status(reason: str) -> int:
    if reason in {"missing_scope_key", "missing_memory_scope"}:
        return 400
    if reason == "principal_mismatch":
        return 401
    return 403


def _api_key_memory_scope_allowed(
    ctx: AuthContext,
    *,
    memory_scope: str,
    scope_key: str | None,
) -> bool:
    allowed_scope_keys = getattr(ctx, "api_key_memory_scope_keys", None)
    if allowed_scope_keys is None:
        return True
    if not isinstance(allowed_scope_keys, list | tuple | set | frozenset):
        return True
    effective_scope_key = ctx.user_id if memory_scope == "private" and not scope_key else scope_key
    return api_key_memory_scope_key(memory_scope, effective_scope_key) in allowed_scope_keys


def _authorize_raw_memory_search(
    *,
    request: SearchRequest,
    ctx: AuthContext,
    accessible_projects: set[str] | None,
) -> None:
    policy_context = MemoryPolicyContext(
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        organization_role=ctx.org_role,
        memory_space=request.memory_scope,
        scope_key=request.scope_key,
        project_id=request.project,
        accessible_projects=accessible_projects,
        source_surface="search",
    )
    decision = authorize_memory_read(policy_context=policy_context)
    if not decision.allowed:
        raise HTTPException(
            status_code=_policy_http_status(decision.reason),
            detail=decision.reason,
        )
    if not _api_key_memory_scope_allowed(
        ctx,
        memory_scope=request.memory_scope,
        scope_key=request.scope_key,
    ):
        raise HTTPException(status_code=403, detail="api_key_memory_space_denied")


@router.post("", response_model=SearchResponse)
async def search(
    request: SearchRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SearchResponse:
    """Unified semantic search across knowledge graph AND documentation.

    Searches both Sibyl's knowledge graph (patterns, rules, episodes, tasks)
    and crawled documentation (via the active content search runtime). Results are merged and ranked
    by relevance score.

    Results are filtered to only include entities from projects the user
    can access, plus unassigned entities.

    Use filters to narrow scope:
    - types: Limit to specific entity types (include 'document' for docs)
    - source_id/source_name: Filter documentation by source
    - include_documents/include_graph: Toggle which stores to search
    """
    started_at = time.perf_counter()
    try:
        from sibyl_core.tools.core import search as core_search

        group_id = str(org.id)

        project_filter = request.project
        if project_filter:
            await verify_entity_project_access(
                None,
                ctx,
                project_filter,
                required_role=ProjectRole.VIEWER,
                require_existing_project=True,
            )
            accessible_projects = None
        else:
            accessible_projects = await list_accessible_project_graph_ids(ctx)

        api_key_memory_scope_keys = getattr(ctx, "api_key_memory_scope_keys", None)
        include_raw_memory = bool(request.include_raw_memory and getattr(ctx, "user_id", None))
        if include_raw_memory:
            raw_accessible_projects = (
                {project_filter}
                if project_filter and accessible_projects is None
                else accessible_projects
            )
            _authorize_raw_memory_search(
                request=request,
                ctx=ctx,
                accessible_projects=raw_accessible_projects,
            )

        # Pass accessible projects to filter results
        # If a specific project is requested, use that; otherwise use accessible set
        result = await core_search(
            query=request.query,
            types=request.types,
            language=request.language,
            category=request.category,
            status=request.status,
            project=project_filter,
            accessible_projects=accessible_projects,
            source=request.source,
            source_id=request.source_id,
            source_name=request.source_name,
            assignee=request.assignee,
            since=request.since,
            reference_time=request.reference_time,
            as_of=request.as_of,
            limit=request.limit,
            offset=request.offset,
            include_content=request.include_content,
            content_max_chars=request.content_max_chars,
            include_documents=request.include_documents,
            include_graph=request.include_graph,
            include_raw_memory=include_raw_memory,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            participants=request.participants,
            labels=request.labels,
            thread_id=request.thread_id,
            occurred_after=request.occurred_after,
            occurred_before=request.occurred_before,
            use_enhanced=request.use_enhanced,
            boost_recent=request.boost_recent,
            organization_id=group_id,
            principal_id=getattr(ctx, "user_id", None),
            allowed_memory_scope_keys=(
                set(api_key_memory_scope_keys) if api_key_memory_scope_keys is not None else None
            ),
        )

        response = SearchResponse(**asdict(result))
        telemetry_registry().record_search_operation(
            surface="search",
            status="ok",
            duration_ms=elapsed_ms(started_at),
            result_count=len(response.results),
        )
        return response

    except HTTPException:
        telemetry_registry().record_search_operation(
            surface="search",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise
    except Exception as e:
        telemetry_registry().record_search_operation(
            surface="search",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        log.exception("search_failed", query=request.query, error=str(e))
        raise HTTPException(status_code=500, detail="Search failed. Please try again.") from e


@router.post("/explore", response_model=ExploreResponse)
async def explore(
    request: ExploreRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> ExploreResponse:
    """Explore and traverse the knowledge graph.

    Results are filtered to only include entities from projects the user
    can access, plus unassigned entities.
    """
    started_at = time.perf_counter()
    try:
        from sibyl_core.tools.core import explore as core_explore

        group_id = str(org.id)

        project_ids = request.project_ids or ([request.project] if request.project else None)

        if request.mode == "dependencies" and request.project_ids:
            raise HTTPException(
                status_code=400,
                detail="dependencies mode does not support project_ids",
            )

        if request.project_ids:
            for project_id in request.project_ids:
                await verify_entity_project_access(
                    None,
                    ctx,
                    project_id,
                    required_role=ProjectRole.VIEWER,
                    require_existing_project=True,
                )
            accessible_filter = (
                set(request.project_ids) if request.mode in {"related", "traverse"} else None
            )
        elif request.project:
            await verify_entity_project_access(
                None,
                ctx,
                request.project,
                required_role=ProjectRole.VIEWER,
                require_existing_project=True,
            )
            accessible_filter = (
                {request.project} if request.mode in {"related", "traverse"} else None
            )
        else:
            accessible_filter = await list_accessible_project_graph_ids(ctx)

        result = await core_explore(
            mode=request.mode,
            types=request.types,
            entity_id=request.entity_id,
            relationship_types=request.relationship_types,
            depth=request.depth,
            language=request.language,
            category=request.category,
            project=request.project if request.mode == "dependencies" else None,
            project_ids=project_ids if request.mode != "dependencies" else None,
            accessible_projects=accessible_filter,
            epic=request.epic,
            no_epic=request.no_epic,
            status=request.status,
            priority=request.priority,
            complexity=request.complexity,
            feature=request.feature,
            tags=request.tags,
            include_archived=request.include_archived,
            limit=request.limit,
            offset=request.offset,
            organization_id=group_id,
        )

        # Convert dataclass to dict, handling nested dataclasses
        entities_list = []
        for entity in result.entities:
            if hasattr(entity, "__dataclass_fields__"):
                entities_list.append(asdict(entity))
            else:
                entities_list.append(entity)

        response = ExploreResponse(
            mode=result.mode,
            entities=entities_list,
            total=result.total,
            filters=result.filters,
            limit=getattr(result, "limit", request.limit),
            offset=getattr(result, "offset", request.offset),
            has_more=getattr(result, "has_more", False),
            actual_total=getattr(result, "actual_total", None),
        )
        telemetry_registry().record_search_operation(
            surface=f"explore_{request.mode}",
            status="ok",
            duration_ms=elapsed_ms(started_at),
            result_count=len(response.entities),
        )
        return response

    except HTTPException:
        telemetry_registry().record_search_operation(
            surface=f"explore_{request.mode}",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        raise
    except Exception as e:
        telemetry_registry().record_search_operation(
            surface=f"explore_{request.mode}",
            status="error",
            duration_ms=elapsed_ms(started_at),
        )
        log.exception("explore_failed", mode=request.mode, error=str(e))
        raise HTTPException(status_code=500, detail="Explore failed. Please try again.") from e


@router.post("/temporal", response_model=TemporalResponse)
@handle_workflow_errors("temporal_query", id_param="entity_id")
async def temporal_query(
    request: TemporalRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> TemporalResponse:
    """Query bi-temporal history of edges.

    Exposes the legacy-compatible bi-temporal edge model for point-in-time queries,
    timeline exploration, and conflict detection.

    Modes:
    - history: Edges as they existed at a point in time (use as_of param)
    - timeline: All versions of edges over time (shows knowledge evolution)
    - conflicts: Find invalidated/superseded facts

    Examples:
    - "What did we know about X in March?" -> mode=history, as_of=2025-03-15
    - "How has knowledge about X evolved?" -> mode=timeline
    - "What facts have been superseded?" -> mode=conflicts
    """
    from sibyl_core.tools.temporal import temporal_query as core_temporal_query

    group_id = str(org.id)

    result = await core_temporal_query(
        mode=request.mode,
        entity_id=request.entity_id,
        as_of=request.as_of,
        include_expired=request.include_expired,
        limit=request.limit,
        organization_id=group_id,
    )

    # Convert dataclass edges to schema objects
    edges_list = [
        TemporalEdgeSchema(
            id=edge.id,
            name=edge.name,
            source_id=edge.source_id,
            source_name=edge.source_name,
            target_id=edge.target_id,
            target_name=edge.target_name,
            created_at=edge.created_at.isoformat() if edge.created_at else None,
            expired_at=edge.expired_at.isoformat() if edge.expired_at else None,
            valid_at=edge.valid_at.isoformat() if edge.valid_at else None,
            invalid_at=edge.invalid_at.isoformat() if edge.invalid_at else None,
            fact=edge.fact,
            is_current=edge.is_current,
        )
        for edge in result.edges
    ]

    return TemporalResponse(
        mode=result.mode,
        entity_id=result.entity_id,
        edges=edges_list,
        total=result.total,
        as_of=result.as_of.isoformat() if result.as_of else None,
        message=result.message,
    )
