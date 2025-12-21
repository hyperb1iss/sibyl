"""Unified search and explore endpoints.

Search endpoint searches both knowledge graph AND crawled documentation,
merging results by relevance score.
"""

from dataclasses import asdict

import structlog
from fastapi import APIRouter, HTTPException

from sibyl.api.schemas import ExploreRequest, ExploreResponse, SearchRequest, SearchResponse

log = structlog.get_logger()
router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(request: SearchRequest) -> SearchResponse:
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

        result = await core_search(
            query=request.query,
            types=request.types,
            language=request.language,
            category=request.category,
            source_id=request.source_id,
            source_name=request.source_name,
            project=request.project,
            status=request.status,
            limit=request.limit,
            include_content=request.include_content,
            include_documents=request.include_documents,
            include_graph=request.include_graph,
        )

        return SearchResponse(**asdict(result))

    except Exception as e:
        log.exception("search_failed", query=request.query, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/explore", response_model=ExploreResponse)
async def explore(request: ExploreRequest) -> ExploreResponse:
    """Explore and traverse the knowledge graph."""
    try:
        from sibyl.tools.core import explore as core_explore

        result = await core_explore(
            mode=request.mode,
            types=request.types,
            entity_id=request.entity_id,
            relationship_types=request.relationship_types,
            depth=request.depth,
            language=request.language,
            category=request.category,
            project=request.project,
            status=request.status,
            limit=request.limit,
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
        )

    except Exception as e:
        log.exception("explore_failed", mode=request.mode, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e
