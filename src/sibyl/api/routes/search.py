"""Search and explore endpoints."""

from dataclasses import asdict

import structlog
from fastapi import APIRouter, HTTPException

from sibyl.api.schemas import ExploreRequest, ExploreResponse, SearchRequest, SearchResponse

log = structlog.get_logger()
router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(request: SearchRequest) -> SearchResponse:
    """Semantic search across the knowledge graph."""
    try:
        from sibyl.tools.core import search as core_search

        result = await core_search(
            query=request.query,
            types=request.types,
            language=request.language,
            category=request.category,
            limit=request.limit,
            include_content=request.include_content,
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
