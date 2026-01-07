"""Unified search, explore, and temporal query endpoints.

Search endpoint searches both knowledge graph AND crawled documentation,
merging results by relevance score. Temporal queries expose bi-temporal
edge metadata for point-in-time queries and conflict detection.
"""

from dataclasses import asdict

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.api.schemas import (
    ExploreRequest,
    ExploreResponse,
    SearchRequest,
    SearchResponse,
    TemporalEdgeSchema,
    TemporalRequest,
    TemporalResponse,
)
from sibyl.auth.authorization import list_accessible_project_graph_ids
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, OrganizationRole

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


@router.post("", response_model=SearchResponse)
async def search(
    request: SearchRequest,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> SearchResponse:
    """Unified semantic search across knowledge graph AND documentation.

    Searches both Sibyl's knowledge graph (patterns, rules, episodes, tasks)
    and crawled documentation (via pgvector). Results are merged and ranked
    by relevance score.

    Results are filtered to only include entities from projects the user
    can access, plus unassigned entities.

    Use filters to narrow scope:
    - types: Limit to specific entity types (include 'document' for docs)
    - source_id/source_name: Filter documentation by source
    - include_documents/include_graph: Toggle which stores to search
    """
    try:
        from sibyl_core.tools.core import search as core_search

        group_id = str(org.id)

        # Get accessible project IDs for filtering
        # Returns None during migration (no projects in Postgres) - skip filtering
        accessible_projects = await list_accessible_project_graph_ids(session, ctx)

        # If user specified a project, validate they have access (if filtering is active)
        project_filter = request.project
        if (
            project_filter
            and accessible_projects is not None
            and project_filter not in accessible_projects
        ):
            raise ProjectAccessDeniedError(
                project_id=project_filter,
                required_role="viewer",
            )

        # Pass accessible projects to filter results
        # None means skip filtering (migration mode)
        # If a specific project is requested, use that; otherwise use accessible set
        result = await core_search(
            query=request.query,
            types=request.types,
            language=request.language,
            category=request.category,
            status=request.status,
            project=project_filter,
            accessible_projects=accessible_projects if not project_filter else None,
            source=request.source,
            source_id=request.source_id,
            source_name=request.source_name,
            assignee=request.assignee,
            since=request.since,
            limit=request.limit,
            offset=request.offset,
            include_content=request.include_content,
            include_documents=request.include_documents,
            include_graph=request.include_graph,
            use_enhanced=request.use_enhanced,
            boost_recent=request.boost_recent,
            organization_id=group_id,
        )

        return SearchResponse(**asdict(result))

    except HTTPException:
        raise
    except Exception as e:
        log.exception("search_failed", query=request.query, error=str(e))
        raise HTTPException(status_code=500, detail="Search failed. Please try again.") from e


@router.post("/explore", response_model=ExploreResponse)
async def explore(
    request: ExploreRequest,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> ExploreResponse:
    """Explore and traverse the knowledge graph.

    Results are filtered to only include entities from projects the user
    can access, plus unassigned entities.
    """
    try:
        from sibyl_core.tools.core import explore as core_explore

        group_id = str(org.id)

        # Get accessible project IDs for filtering
        # Returns None during migration (no projects in Postgres) - skip filtering
        accessible_projects = await list_accessible_project_graph_ids(session, ctx)

        # If user specified a project, validate they have access (if filtering is active)
        project_filter = request.project
        if (
            project_filter
            and accessible_projects is not None
            and project_filter not in accessible_projects
        ):
            raise ProjectAccessDeniedError(
                project_id=project_filter,
                required_role="viewer",
            )

        result = await core_explore(
            mode=request.mode,
            types=request.types,
            entity_id=request.entity_id,
            relationship_types=request.relationship_types,
            depth=request.depth,
            language=request.language,
            category=request.category,
            project=project_filter,
            accessible_projects=accessible_projects if not project_filter else None,
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

        return ExploreResponse(
            mode=result.mode,
            entities=entities_list,
            total=result.total,
            filters=result.filters,
            limit=getattr(result, "limit", request.limit),
            offset=getattr(result, "offset", request.offset),
            has_more=getattr(result, "has_more", False),
            actual_total=getattr(result, "actual_total", None),
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("explore_failed", mode=request.mode, error=str(e))
        raise HTTPException(status_code=500, detail="Explore failed. Please try again.") from e


@router.post("/temporal", response_model=TemporalResponse)
async def temporal_query(
    request: TemporalRequest,
    org: Organization = Depends(get_current_organization),
) -> TemporalResponse:
    """Query bi-temporal history of edges.

    Exposes Graphiti's bi-temporal model for point-in-time queries,
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
    try:
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

    except HTTPException:
        raise
    except Exception as e:
        log.exception("temporal_query_failed", mode=request.mode, error=str(e))
        raise HTTPException(
            status_code=500, detail="Temporal query failed. Please try again."
        ) from e
