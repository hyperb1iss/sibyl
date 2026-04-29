"""Search tool for unified semantic search across Sibyl knowledge graph and documentation."""

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import structlog

from sibyl_core.models.entities import EntityType
from sibyl_core.retrieval import HybridConfig, hybrid_search, temporal_boost
from sibyl_core.retrieval.fusion import rrf_merge
from sibyl_core.services import document_search as document_search_service
from sibyl_core.services import get_graph_runtime as _service_get_graph_runtime
from sibyl_core.tools.helpers import (
    VALID_ENTITY_TYPES,
    _build_entity_metadata,
    _get_field,
    _serialize_enum,
)
from sibyl_core.tools.responses import SearchResponse, SearchResult
from sibyl_core.utils.log_safety import query_log_fields
from sibyl_core.utils.resilience import TIMEOUTS, with_timeout

log = structlog.get_logger()

DOCUMENT_SEARCH_TIMEOUT_SECONDS = min(10.0, TIMEOUTS["search"])
DOCUMENT_SEARCH_GRAPH_JOIN_TIMEOUT_SECONDS = min(2.0, DOCUMENT_SEARCH_TIMEOUT_SECONDS)


async def get_graph_runtime(group_id: str):
    return await _service_get_graph_runtime(group_id)


__all__ = [
    "_dedupe_document_rows",
    "_document_language_predicates",
    "_merge_document_results",
    "search",
]


def _graph_result_key(result: tuple[Any, float]) -> str:
    entity, _score = result
    return str(getattr(entity, "id", None) or getattr(entity, "uuid", None) or id(entity))


def _graph_results_contain_exact_name_match(
    results: Sequence[tuple[Any, float]],
    query: str,
) -> bool:
    normalized_query = query.strip().lower()
    if not normalized_query:
        return False

    return any(
        str(getattr(entity, "name", "")).strip().lower() == normalized_query
        for entity, _score in results
    )


def _matches_requested_graph_type(entity: Any, requested_graph_types: set[str]) -> bool:
    entity_type = str(_serialize_enum(_get_field(entity, "entity_type", "")))
    return entity_type.lower() in requested_graph_types


def _merge_graph_results(
    prioritized_results: Sequence[tuple[Any, float]],
    secondary_results: Sequence[tuple[Any, float]],
    limit: int,
) -> list[tuple[Any, float]]:
    seen_ids: set[str] = set()
    merged: list[tuple[Any, float]] = []

    for result in [*prioritized_results, *secondary_results]:
        key = _graph_result_key(result)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        merged.append(result)
        if len(merged) >= limit:
            break

    return merged


def _has_graph_list_filters(
    *,
    entity_types: list[EntityType] | None,
    language: str | None,
    category: str | None,
    status: str | None,
    project: str | None,
    source: str | None,
    assignee: str | None,
    since: str | None,
) -> bool:
    return any(
        (
            entity_types,
            language,
            category,
            status,
            project,
            source,
            assignee,
            since,
        )
    )


async def _list_graph_entities_for_filters(
    entity_manager: Any,
    *,
    entity_types: list[EntityType] | None,
    limit: int,
    offset: int,
    project: str | None,
    language: str | None,
    category: str | None,
    status: str | None,
    source: str | None,
    assignee: str | None,
    since_date: datetime | None,
    accessible_projects: set[str] | None,
) -> list[tuple[Any, float]]:
    target_count = max(limit + offset, limit)
    page_size = min(max(target_count, 1), 500)
    matched: list[tuple[Any, float]] = []

    def accepts(entity: Any) -> bool:
        return _matches_graph_filters(
            entity,
            language=language,
            category=category,
            status=status,
            project=project,
            source=source,
            assignee=assignee,
            since_date=since_date,
            accessible_projects=accessible_projects,
        )

    if entity_types:
        for entity_type in entity_types:
            page_offset = 0
            while len(matched) < target_count:
                entities = await entity_manager.list_by_type(
                    entity_type,
                    limit=page_size,
                    offset=page_offset,
                    project_id=project,
                    status=status,
                )
                if not entities:
                    break
                matched.extend((entity, 1.0) for entity in entities if accepts(entity))
                if len(entities) < page_size:
                    break
                page_offset += len(entities)
        return matched

    page_offset = 0
    while len(matched) < target_count:
        entities = await entity_manager.list_all(limit=page_size, offset=page_offset)
        if not entities:
            break
        matched.extend((entity, 1.0) for entity in entities if accepts(entity))
        if len(entities) < page_size:
            break
        page_offset += len(entities)
    return matched


def _matches_graph_filters(
    entity: Any,
    *,
    language: str | None,
    category: str | None,
    status: str | None,
    project: str | None,
    source: str | None,
    assignee: str | None,
    since_date: datetime | None,
    accessible_projects: set[str] | None,
) -> bool:
    if language:
        entity_langs = _get_field(entity, "languages", [])
        if language.lower() not in [lang.lower() for lang in entity_langs]:
            return False

    if category:
        entity_cat = _get_field(entity, "category", "")
        if category.lower() not in entity_cat.lower():
            return False

    if status:
        entity_status = _get_field(entity, "status")
        if entity_status is None:
            return False
        status_val = str(_serialize_enum(entity_status)).lower()
        status_list = [s.strip().lower() for s in status.split(",")]
        if status_val not in status_list:
            return False

    entity_project = _get_field(entity, "project_id")
    if project and entity_project is not None and entity_project != project:
        return False

    if accessible_projects is not None:
        entity_project = _get_field(entity, "project_id")
        if entity_project is not None and entity_project not in accessible_projects:
            return False

    if source and _get_field(entity, "source_id") != source:
        return False

    if assignee:
        entity_assignees = _get_field(entity, "assignees", [])
        if assignee.lower() not in [a.lower() for a in entity_assignees]:
            return False

    if since_date:
        entity_created = _get_field(entity, "created_at")
        if entity_created:
            try:
                if isinstance(entity_created, str):
                    entity_created = datetime.fromisoformat(entity_created)
                if entity_created < since_date:
                    return False
            except (ValueError, TypeError):
                pass

    return True


def _document_language_predicates(
    *,
    language: str | None,
    chunk_type_column: Any,
    language_column: Any,
    code_chunk_type: Any,
) -> tuple[Any, ...]:
    return document_search_service._document_language_predicates(
        language=language,
        chunk_type_column=chunk_type_column,
        language_column=language_column,
        code_chunk_type=code_chunk_type,
    )


def _dedupe_document_rows(
    rows: Sequence[Any],
) -> list[tuple[Any, Any, str, Any, float]]:
    return document_search_service._dedupe_document_rows(rows)


def _merge_document_results(
    vector_results: list[SearchResult],
    lexical_results: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    return document_search_service._merge_document_results(
        vector_results=vector_results,
        lexical_results=lexical_results,
        limit=limit,
    )


def _rank_fuse_search_results(
    graph_results: Sequence[SearchResult],
    doc_results: Sequence[SearchResult],
) -> list[SearchResult]:
    if not graph_results or not doc_results:
        return sorted([*graph_results, *doc_results], key=lambda r: r.score, reverse=True)

    ranked_sources = [
        [
            (result, result.score)
            for result in sorted(graph_results, key=lambda r: r.score, reverse=True)
        ],
        [
            (result, result.score)
            for result in sorted(doc_results, key=lambda r: r.score, reverse=True)
        ],
    ]
    return [result for result, _score in rrf_merge(ranked_sources, dedup_key=lambda r: r.id)]


async def _search_documents(
    query: str,
    organization_id: str,
    source_id: str | None = None,
    source_name: str | None = None,
    language: str | None = None,
    limit: int = 10,
    include_content: bool = True,
) -> list[SearchResult]:
    try:
        return await document_search_service.search_documents(
            query=query,
            organization_id=organization_id,
            source_id=source_id,
            source_name=source_name,
            language=language,
            limit=limit,
            include_content=include_content,
        )
    except Exception as e:
        log.warning("document_search_failed", error_type=type(e).__name__)
        return []


async def search(
    query: str,
    types: list[str] | None = None,
    language: str | None = None,
    category: str | None = None,
    status: str | None = None,
    project: str | None = None,
    accessible_projects: set[str] | None = None,
    source: str | None = None,
    source_id: str | None = None,
    source_name: str | None = None,
    assignee: str | None = None,
    since: str | None = None,
    limit: int = 10,
    offset: int = 0,
    include_content: bool = True,
    include_documents: bool = True,
    include_graph: bool = True,
    use_enhanced: bool = True,
    boost_recent: bool = True,
    temporal_decay_days: float | None = None,
    organization_id: str | None = None,
) -> SearchResponse:
    """Unified semantic search across knowledge graph AND documentation.

    Searches both Sibyl's knowledge graph (patterns, rules, episodes, tasks)
    AND crawled documentation (pgvector similarity search). Results are
    merged and ranked by relevance score.

    TASK MANAGEMENT WORKFLOW:
    For task searches, always include project filter:
    1. First: explore(mode="list", types=["project"]) - Identify the project
    2. Then: search("query", types=["task"], project="<project_id>") - Search within project

    USE CASES:
    • Find patterns/rules: search("OAuth authentication best practices")
    • Search documentation: search("Next.js middleware", source_name="next-dynenv")
    • Find tasks: search("", types=["task"], project="proj_abc", status="todo")
    • Search by language: search("async patterns", language="python")
    • Documentation only: search("hooks", include_graph=False)
    • Graph only: search("debugging", include_documents=False)

    Args:
        query: Natural language search query. Required.
        types: Entity types to search. Options: pattern, rule, template, topic,
               episode, task, project, document. Include 'document' to search docs.
        language: Filter by programming language (python, typescript, etc.).
        category: Filter by category/domain (authentication, database, api, etc.).
        status: Filter tasks by workflow status (backlog, todo, doing, etc.).
        project: Filter by project_id for tasks.
        source: Filter graph entities by source_id.
        source_id: Filter documents by source UUID.
        source_name: Filter documents by source name (partial match).
        assignee: Filter tasks by assignee name.
        since: Temporal filter - only return entities created after this ISO date.
        limit: Maximum results to return (1-50, default 10).
        offset: Offset for pagination (default 0).
        include_content: Include full content in results (default True).
        include_documents: Include crawled documentation in search (default True).
        include_graph: Include knowledge graph entities in search (default True).
        use_enhanced: Use enhanced hybrid retrieval for graph (default True).
        boost_recent: Apply temporal boosting for graph results (default True).

    Returns:
        SearchResponse with ranked results from both sources, including
        graph_count and document_count for result breakdown.

    EXAMPLES:
        search("error handling patterns", types=["pattern"], language="python")
        search("Next.js routing", source_name="next-dynenv")
        search("", types=["task"], status="todo", project="proj_auth")
    """
    # Clamp limit and offset
    limit = max(1, min(limit, 50))
    offset = max(0, offset)

    log.info(
        "unified_search",
        **query_log_fields(query),
        types=types,
        language=language,
        category=category,
        status=status,
        project=project,
        source_id=source_id,
        source_name=source_name,
        include_documents=include_documents,
        include_graph=include_graph,
        limit=limit,
    )

    filters = {}
    if types:
        filters["types"] = types
    if language:
        filters["language"] = language
    if category:
        filters["category"] = category
    if status:
        filters["status"] = status
    if project:
        filters["project"] = project
    if source:
        filters["source"] = source
    if source_id:
        filters["source_id"] = source_id
    if source_name:
        filters["source_name"] = source_name
    if assignee:
        filters["assignee"] = assignee
    if since:
        filters["since"] = since

    # Determine if we should search documents based on types filter
    search_documents = include_documents
    search_graph = include_graph
    if types:
        # If 'document' is in types, search documents
        # If only 'document' is in types, skip graph search
        type_set = {t.lower() for t in types}
        if "document" in type_set:
            search_documents = include_documents
            if type_set == {"document"}:
                search_graph = False
        elif source_id or source_name:
            # If source filters are set but document not in types, add document search
            search_documents = include_documents
        else:
            # Types specified but document not included - skip document search
            search_documents = False

    graph_results: list[SearchResult] = []
    doc_results: list[SearchResult] = []
    document_search_task: asyncio.Task[list[SearchResult]] | None = None
    if search_documents and query and organization_id:
        document_timeout_seconds = (
            DOCUMENT_SEARCH_GRAPH_JOIN_TIMEOUT_SECONDS
            if search_graph
            else DOCUMENT_SEARCH_TIMEOUT_SECONDS
        )
        document_search_task = asyncio.create_task(
            with_timeout(
                _search_documents(
                    query=query,
                    organization_id=organization_id,
                    source_id=source_id,
                    source_name=source_name,
                    language=language,
                    limit=limit,
                    include_content=include_content,
                ),
                timeout_seconds=document_timeout_seconds,
                operation_name="document_search",
            )
        )

    requested_graph_types: set[str] = set()
    entity_types = None
    if types:
        entity_types = []
        for t in types:
            if t.lower() in VALID_ENTITY_TYPES and t.lower() != "document":
                requested_graph_types.add(t.lower())
                entity_types.append(EntityType(t.lower()))

    graph_list_filters = _has_graph_list_filters(
        entity_types=entity_types,
        language=language,
        category=category,
        status=status,
        project=project,
        source=source,
        assignee=assignee,
        since=since,
    )

    # =========================================================================
    # GRAPH SEARCH - Search knowledge graph entities
    # =========================================================================
    if search_graph and (query or graph_list_filters):
        try:
            if not organization_id:
                raise ValueError(
                    "organization_id is required - cannot access graph without org context"
                )
            runtime = await get_graph_runtime(organization_id)
            client = runtime.client
            entity_manager = runtime.entity_manager

            # Parse since date if provided
            since_date = None
            if since:
                try:
                    since_date = datetime.fromisoformat(since)
                except ValueError:
                    log.warning("invalid_since_date", since=since)

            # Perform search - try enhanced hybrid first, then fall back to
            # Graphiti's node-hybrid search directly.
            raw_results: list[tuple[Any, float]] = []
            enhanced_search_exhausted = False

            if query and use_enhanced:
                try:
                    from sibyl_core.config import core_config

                    decay = temporal_decay_days or core_config.temporal_decay_days
                    hybrid_config = HybridConfig(
                        apply_temporal=boost_recent,
                        temporal_decay_days=decay,
                        graph_depth=2,
                        apply_reranking=core_config.rerank_enabled,
                        rerank_top_k=core_config.rerank_top_k,
                        rerank_model=core_config.rerank_model,
                    )

                    hybrid_result = await with_timeout(
                        hybrid_search(
                            query=query,
                            client=client,
                            entity_manager=entity_manager,
                            entity_types=entity_types,
                            limit=limit * 3,
                            config=hybrid_config,
                            group_id=organization_id,
                        ),
                        timeout_seconds=TIMEOUTS["search"],
                        operation_name="hybrid_search",
                    )
                    raw_results = hybrid_result.results
                    enhanced_search_exhausted = bool(
                        hybrid_result.metadata.get("entity_manager_search_completed")
                    )
                    log.debug("graph_search_enhanced", results=len(raw_results))

                except Exception as e:
                    log.warning("enhanced_search_failed_fallback", error_type=type(e).__name__)

            # Fall back to Graphiti's node-hybrid search
            if query and not raw_results and not enhanced_search_exhausted:
                try:
                    raw_results = await with_timeout(
                        entity_manager.search(
                            query=query,
                            entity_types=entity_types,
                            limit=limit * 3,
                        ),
                        timeout_seconds=TIMEOUTS["search"],
                        operation_name="search",
                    )
                    if boost_recent and raw_results:
                        from sibyl_core.config import core_config

                        decay = temporal_decay_days or core_config.temporal_decay_days
                        raw_results = temporal_boost(raw_results, decay_days=decay)
                except Exception as e:
                    log.warning("fallback_graph_search_failed", error_type=type(e).__name__)
                    raw_results = []

            if (
                query
                and
                not enhanced_search_exhausted
                and not _graph_results_contain_exact_name_match(raw_results, query)
            ):
                try:
                    exact_name_results = await with_timeout(
                        entity_manager.search_exact_name(
                            query=query,
                            entity_types=entity_types,
                            limit=limit,
                        ),
                        timeout_seconds=TIMEOUTS["search"],
                        operation_name="search_exact_name",
                    )
                    if exact_name_results:
                        raw_results = _merge_graph_results(
                            exact_name_results,
                            raw_results,
                            limit=limit * 3,
                        )
                except Exception as e:
                    log.warning("graph_exact_name_search_failed", error_type=type(e).__name__)

            if not query and graph_list_filters:
                raw_results = await _list_graph_entities_for_filters(
                    entity_manager,
                    entity_types=entity_types,
                    limit=limit,
                    offset=offset,
                    project=project,
                    language=language,
                    category=category,
                    status=status,
                    source=source,
                    assignee=assignee,
                    since_date=since_date,
                    accessible_projects=accessible_projects,
                )

            if requested_graph_types:
                typed_results = [
                    (entity, score)
                    for entity, score in raw_results
                    if _matches_requested_graph_type(entity, requested_graph_types)
                ]
            else:
                typed_results = raw_results

            if query and not typed_results and requested_graph_types:
                try:
                    fallback_results = await with_timeout(
                        entity_manager.search(
                            query=query,
                            entity_types=None,
                            limit=limit * 3,
                        ),
                        timeout_seconds=TIMEOUTS["search"],
                        operation_name="search_untyped_fallback",
                    )
                except Exception as e:
                    log.warning("untyped_graph_search_failed", error_type=type(e).__name__)
                    fallback_results = []
                typed_results = [
                    (entity, score)
                    for entity, score in fallback_results
                    if _matches_requested_graph_type(entity, requested_graph_types)
                ]

            raw_results = typed_results

            # Filter and convert to SearchResult
            for entity, score in raw_results:
                if not _matches_graph_filters(
                    entity,
                    language=language,
                    category=category,
                    status=status,
                    project=project,
                    source=source,
                    assignee=assignee,
                    since_date=since_date,
                    accessible_projects=accessible_projects,
                ):
                    continue

                content = ""
                if include_content:
                    content = entity.content[:500] if entity.content else entity.description
                else:
                    content = entity.description[:200] if entity.description else ""

                graph_results.append(
                    SearchResult(
                        id=entity.id,
                        type=entity.entity_type.value,
                        name=entity.name,
                        content=content or "",
                        score=score,
                        source=entity.source_file,
                        result_origin="graph",
                        metadata=_build_entity_metadata(entity),
                    )
                )

                if len(graph_results) >= offset + limit:
                    break

        except Exception as e:
            log.warning("graph_search_failed", error_type=type(e).__name__)

    # =========================================================================
    # DOCUMENT SEARCH - Search crawled documentation
    # =========================================================================
    if document_search_task is not None:
        try:
            doc_results = await document_search_task
            log.debug("document_search_complete", results=len(doc_results))
        except Exception as e:
            log.warning("document_search_failed", error_type=type(e).__name__)

    # =========================================================================
    # MERGE AND RANK RESULTS
    # =========================================================================
    # Deduplicate by ID, keeping highest score for each entity
    seen_ids: dict[str, SearchResult] = {}
    for result in graph_results + doc_results:
        if result.id not in seen_ids or result.score > seen_ids[result.id].score:
            seen_ids[result.id] = result

    deduped_graph_results = [
        result
        for result in graph_results
        if seen_ids.get(result.id) is result
    ]
    deduped_doc_results = [
        result
        for result in doc_results
        if seen_ids.get(result.id) is result
    ]

    all_results = _rank_fuse_search_results(deduped_graph_results, deduped_doc_results)

    # Apply pagination
    total_count = len(all_results)
    paginated_results = all_results[offset : offset + limit]
    has_more = offset + len(paginated_results) < total_count

    return SearchResponse(
        results=paginated_results,
        total=total_count,
        query=query,
        filters=filters,
        graph_count=len([r for r in paginated_results if r.result_origin == "graph"]),
        document_count=len([r for r in paginated_results if r.result_origin == "document"]),
        limit=limit,
        offset=offset,
        has_more=has_more,
    )
