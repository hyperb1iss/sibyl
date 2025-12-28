"""Unified search and explore endpoints.

Search endpoint searches both knowledge graph AND crawled documentation,
merging results by relevance score.
"""

from dataclasses import asdict

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import ExploreRequest, ExploreResponse, SearchRequest, SearchResponse
from sibyl.auth.dependencies import get_current_organization, require_org_role
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
) -> SearchResponse:
    """Unified semantic search across knowledge graph AND documentation.

    Searches both Sibyl's knowledge graph (patterns, rules, episodes, tasks)
    and crawled documentation (via pgvector). Results are merged and ranked
    by relevance score.

    Use filters to narrow scope:
    - types: Limit to specific entity types (include 'document' for docs)
    - source_id/source_name: Filter documentation by source
    - include_documents/include_graph: Toggle which stores to search
    """
    try:
        from sibyl.tools.core import search as core_search

        group_id = str(org.id)
        result = await core_search(
            query=request.query,
            types=request.types,
            language=request.language,
            category=request.category,
            status=request.status,
            project=request.project,
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

    except Exception as e:
        log.exception("search_failed", query=request.query, error=str(e))
        raise HTTPException(status_code=500, detail="Search failed. Please try again.") from e


@router.post("/explore", response_model=ExploreResponse)
async def explore(
    request: ExploreRequest,
    org: Organization = Depends(get_current_organization),
) -> ExploreResponse:
    """Explore and traverse the knowledge graph."""
    try:
        from sibyl.tools.core import explore as core_explore

        group_id = str(org.id)
        result = await core_explore(
            mode=request.mode,
            types=request.types,
            entity_id=request.entity_id,
            relationship_types=request.relationship_types,
            depth=request.depth,
            language=request.language,
            category=request.category,
            project=request.project,
            epic=request.epic,
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

    except Exception as e:
        log.exception("explore_failed", mode=request.mode, error=str(e))
        raise HTTPException(status_code=500, detail="Explore failed. Please try again.") from e
