"""Unified search and explore endpoints.

Search endpoint searches both knowledge graph AND crawled documentation,
merging results by relevance score.
"""

from dataclasses import asdict

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.api.schemas import ExploreRequest, ExploreResponse, SearchRequest, SearchResponse
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
        if project_filter and accessible_projects is not None and project_filter not in accessible_projects:
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
        if project_filter and accessible_projects is not None and project_filter not in accessible_projects:
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
