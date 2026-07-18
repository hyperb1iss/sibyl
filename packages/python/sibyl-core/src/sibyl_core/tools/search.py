"""Search tool for unified semantic search across Sibyl knowledge graph and documentation."""

import asyncio
import time
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.embeddings.providers import configured_embedding_provider
from sibyl_core.memory_pipeline.retrieval import CandidateSourceFailure
from sibyl_core.models.entities import EntityType
from sibyl_core.retrieval import HybridConfig, hybrid_search, temporal_boost
from sibyl_core.retrieval.candidates import (
    CandidateKind,
    CandidateScope,
    CandidateSignal,
    candidate_contract_metadata,
)
from sibyl_core.retrieval.fusion import rrf_merge
from sibyl_core.retrieval.temporal import parse_temporal_datetime
from sibyl_core.services import document_search as document_search_service
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    RawMemoryRecallResult,
)
from sibyl_core.services.surreal_content import (
    recall_raw_memory_with_sources as recall_raw_memory,
)
from sibyl_core.tools.helpers import (
    VALID_ENTITY_TYPES,
    _build_entity_metadata,
    _get_field,
    _project_id_for_policy,
    _serialize_enum,
)
from sibyl_core.tools.responses import SearchResponse, SearchResult
from sibyl_core.tools.usage_exposure import (
    _USAGE_EXPOSURE_SUMMARY_KEY,
    annotate_search_result_exposures,
)
from sibyl_core.utils.log_safety import query_log_fields
from sibyl_core.utils.resilience import TIMEOUTS, with_timeout

log = structlog.get_logger()

DOCUMENT_SEARCH_TIMEOUT_SECONDS = min(10.0, TIMEOUTS["search"])
DOCUMENT_SEARCH_GRAPH_JOIN_TIMEOUT_SECONDS = min(2.0, DOCUMENT_SEARCH_TIMEOUT_SECONDS)
DEFAULT_SEARCH_CONTENT_MAX_CHARS = 500
MAX_SEARCH_CONTENT_MAX_CHARS = 50_000
SEARCH_PREVIEW_MAX_CHARS = 200


async def _timed_search_source(
    operation: Awaitable[Any],
    *,
    timings: dict[str, float],
    stage: str,
) -> Any:
    started_at = time.perf_counter()
    try:
        return await operation
    finally:
        timings[stage] = (time.perf_counter() - started_at) * 1000.0


@dataclass(frozen=True, slots=True)
class RawMemorySearchFetch:
    results: list[SearchResult]
    failures: tuple[CandidateSourceFailure, ...]
    metadata: dict[str, Any]


async def get_graph_runtime(group_id: str):
    from sibyl_core.services.graph import get_surreal_graph_runtime

    kwargs = {
        "embedding_provider": configured_embedding_provider(),
        "ensure_schema": False,
    }
    try:
        return await get_surreal_graph_runtime(group_id, **kwargs)
    except TypeError as error:
        if "unexpected keyword argument 'ensure_schema'" not in str(error):
            raise
        kwargs.pop("ensure_schema")
        return await get_surreal_graph_runtime(group_id, **kwargs)


__all__ = [
    "_dedupe_document_rows",
    "_merge_document_results",
    "graph_entity_to_search_result",
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
    try:
        entity_type = EntityType(entity_type.lower()).value
    except ValueError:
        entity_type = entity_type.lower()
    return entity_type in requested_graph_types


def _graph_candidate_metadata(
    entity: Any,
    *,
    organization_id: str,
    principal_id: str | None,
    policy_reason: str,
) -> dict[str, Any]:
    metadata = _build_entity_metadata(entity)
    memory_scope = metadata.get("memory_scope")
    project_id = _project_id_for_policy(entity)
    visibility = str(memory_scope or ("project" if project_id else "organization"))
    return candidate_contract_metadata(
        kind=CandidateKind.NODE,
        signals=[CandidateSignal.HYBRID.value],
        scope=CandidateScope(
            organization_id=organization_id,
            project_id=project_id,
            memory_scope=str(memory_scope) if memory_scope else None,
            scope_key=str(metadata["scope_key"]) if metadata.get("scope_key") else None,
            principal_id=str(principal_id or metadata["principal_id"])
            if principal_id or metadata.get("principal_id")
            else None,
            visibility=visibility,
            policy_reason=policy_reason,
        ),
        metadata=metadata,
    )


def graph_entity_to_search_result(
    entity: Any,
    *,
    organization_id: str,
    principal_id: str | None,
    score: float,
    include_content: bool = True,
    content_max_chars: int = DEFAULT_SEARCH_CONTENT_MAX_CHARS,
    policy_reason: str = "search_scope_verified",
) -> SearchResult:
    """Convert an authorized graph entity through the standard search contract."""
    if include_content:
        content = (entity.content or entity.description or "")[:content_max_chars]
    else:
        content = entity.description[:SEARCH_PREVIEW_MAX_CHARS] if entity.description else ""
    return SearchResult(
        id=entity.id,
        type=entity.entity_type.value,
        name=entity.name,
        content=content,
        score=score,
        source=entity.source_file,
        result_origin="graph",
        metadata=_graph_candidate_metadata(
            entity,
            organization_id=organization_id,
            principal_id=principal_id,
            policy_reason=policy_reason,
        ),
    )


def _document_candidate_metadata(
    result: SearchResult,
    *,
    organization_id: str,
) -> dict[str, Any]:
    return candidate_contract_metadata(
        kind=CandidateKind.DOCUMENT,
        signals=result.metadata.get("retrieval_signals") or (),
        scope=CandidateScope(
            organization_id=organization_id,
            visibility="organization",
            policy_reason="content_scope_verified",
        ),
        metadata=result.metadata,
    )


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


def _has_graph_only_filters(
    *,
    category: str | None,
    status: str | None,
    project: str | None,
    source: str | None,
    assignee: str | None,
    since: str | None,
) -> bool:
    return any((category, status, project, source, assignee, since))


def _normalize_temporal_datetime(value: str | datetime | None) -> datetime | None:
    parsed = parse_temporal_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _matches_as_of(entity: Any, as_of: datetime | None) -> bool:
    if as_of is None:
        return True

    created_at = _normalize_temporal_datetime(_get_field(entity, "created_at"))
    if created_at is not None and created_at > as_of:
        return False

    for field in ("valid_at", "valid_from"):
        valid_at = _normalize_temporal_datetime(_get_field(entity, field))
        if valid_at is not None and valid_at > as_of:
            return False

    for field in ("invalid_at", "valid_to"):
        invalid_at = _normalize_temporal_datetime(_get_field(entity, field))
        if invalid_at is not None and invalid_at <= as_of:
            return False

    return True


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
    as_of: datetime | None,
    accessible_projects: set[str] | None,
    principal_id: str | None,
    allowed_memory_scope_keys: set[str] | None,
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
            principal_id=principal_id,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            source=source,
            assignee=assignee,
            since_date=since_date,
            as_of=as_of,
            accessible_projects=accessible_projects,
        )

    if entity_types:
        for entity_type in entity_types:
            page_offset = 0
            while len(matched) < target_count:
                query_project_id = None if entity_type == EntityType.PROJECT else project
                entities = await entity_manager.list_by_type(
                    entity_type,
                    limit=page_size,
                    offset=page_offset,
                    project_id=query_project_id,
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
    principal_id: str | None,
    allowed_memory_scope_keys: set[str] | None,
    source: str | None,
    assignee: str | None,
    since_date: datetime | None,
    as_of: datetime | None = None,
    accessible_projects: set[str] | None,
) -> bool:
    if not _matches_memory_scope_policy(
        entity,
        project=project,
        principal_id=principal_id,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        accessible_projects=accessible_projects,
    ):
        return False

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

    entity_project = _project_id_for_policy(entity)
    if project and entity_project is not None and entity_project != project:
        return False

    if (
        accessible_projects is not None
        and entity_project is not None
        and entity_project not in accessible_projects
    ):
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

    return _matches_as_of(entity, as_of)


def _matches_memory_scope_policy(
    entity: Any,
    *,
    project: str | None,
    principal_id: str | None,
    allowed_memory_scope_keys: set[str] | None,
    accessible_projects: set[str] | None,
) -> bool:
    metadata = getattr(entity, "metadata", {}) or {}
    raw_scope = metadata.get("memory_scope")
    if raw_scope is None:
        return True

    memory_scope = str(raw_scope).strip()
    scope_key = metadata.get("scope_key")
    effective_scope_key = principal_id if memory_scope == "private" else scope_key

    if allowed_memory_scope_keys is not None:
        return (
            memory_scope_policy_key(memory_scope, effective_scope_key) in allowed_memory_scope_keys
        )

    if memory_scope == "private":
        owner = metadata.get("principal_id") or scope_key
        return bool(principal_id) and str(owner) == str(principal_id)

    if memory_scope == "project":
        scoped_project = str(scope_key or metadata.get("project_id") or "")
        if project:
            return scoped_project == project
        if accessible_projects is not None:
            return scoped_project in accessible_projects

    return True


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
    raw_memory_results: Sequence[SearchResult] = (),
) -> list[SearchResult]:
    result_groups = [graph_results, doc_results, raw_memory_results]
    non_empty_groups = [results for results in result_groups if results]
    if len(non_empty_groups) < 2:
        return sorted(
            [result for results in non_empty_groups for result in results],
            key=lambda r: r.score,
            reverse=True,
        )

    ranked_sources = [
        [
            (result, result.score)
            for result in sorted(graph_results, key=lambda r: r.score, reverse=True)
        ],
        [
            (result, result.score)
            for result in sorted(doc_results, key=lambda r: r.score, reverse=True)
        ],
        [
            (result, result.score)
            for result in sorted(raw_memory_results, key=lambda r: r.score, reverse=True)
        ],
    ]
    ranked_sources = [source for source in ranked_sources if source]
    return [result for result, _score in rrf_merge(ranked_sources, dedup_key=lambda r: r.id)]


def _record_source_failure(
    failures: list[CandidateSourceFailure],
    *,
    source: str,
    error: Exception,
) -> None:
    failures.append(CandidateSourceFailure(source=source, error_type=type(error).__name__))


def _add_source_failure_filters(
    filters: dict[str, Any],
    failures: Sequence[CandidateSourceFailure],
) -> None:
    filters["search_source_degraded"] = bool(failures)
    filters["search_source_failure_count"] = len(failures)
    if failures:
        filters["search_source_failures"] = [failure.as_metadata() for failure in failures]


async def _search_documents(
    query: str,
    organization_id: str,
    source_id: str | None = None,
    source_name: str | None = None,
    language: str | None = None,
    limit: int = 10,
    include_content: bool = True,
    content_max_chars: int = DEFAULT_SEARCH_CONTENT_MAX_CHARS,
) -> list[SearchResult]:
    return await document_search_service.search_documents(
        query=query,
        organization_id=organization_id,
        source_id=source_id,
        source_name=source_name,
        language=language,
        limit=limit,
        include_content=include_content,
        content_max_chars=content_max_chars,
    )


def _raw_memory_search_result(
    memory: RawMemory,
    *,
    organization_id: str,
    include_content: bool,
    content_max_chars: int,
) -> SearchResult:
    source = memory.source_id or memory.capture_surface
    project_id = (
        memory.metadata.get("project_id")
        or memory.project_id
        or (memory.scope_key if memory.memory_scope is MemoryScope.PROJECT else None)
    )
    metadata = candidate_contract_metadata(
        kind=CandidateKind.RAW_MEMORY,
        signals=[CandidateSignal.RAW_LEXICAL.value],
        scope=CandidateScope(
            organization_id=organization_id,
            project_id=str(project_id) if project_id is not None else None,
            memory_scope=memory.memory_scope.value,
            scope_key=memory.scope_key,
            principal_id=memory.principal_id,
            visibility=memory.memory_scope.value,
            policy_reason="search_scope_verified",
        ),
        metadata={
            "source_id": source,
            "principal_id": memory.principal_id,
            "memory_scope": memory.memory_scope.value,
            "scope_key": memory.scope_key,
            "capture_surface": memory.capture_surface,
            "tags": list(memory.tags),
            **memory.metadata,
        },
    )
    if include_content:
        content = (memory.snippet or memory.raw_content)[:content_max_chars]
    else:
        content = (memory.snippet or memory.raw_content)[:SEARCH_PREVIEW_MAX_CHARS]

    return SearchResult(
        id=f"raw_memory:{memory.id}",
        type="raw_memory",
        name=memory.title or "Untitled raw memory",
        content=content,
        score=memory.score,
        source=source,
        result_origin="raw_memory",
        metadata=metadata,
    )


async def _search_raw_memories(
    *,
    query: str,
    organization_id: str,
    principal_id: str,
    memory_scope: str,
    scope_key: str | None,
    project_id: str | None,
    source_id: str | None,
    participants: Sequence[str] | None,
    labels: Sequence[str] | None,
    thread_id: str | None,
    occurred_after: datetime | str | None,
    occurred_before: datetime | str | None,
    as_of: datetime | str | None,
    limit: int,
    include_content: bool,
    content_max_chars: int,
) -> RawMemorySearchFetch:
    recall_result = await recall_raw_memory(
        organization_id=organization_id,
        principal_id=principal_id,
        query=query,
        memory_scope=memory_scope,
        scope_key=scope_key,
        project_id=project_id,
        source_ids=[source_id] if source_id else None,
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        as_of=as_of,
        limit=limit,
    )
    if isinstance(recall_result, RawMemoryRecallResult):
        memories = recall_result.memories
        failures = recall_result.failures
        metadata = recall_result.as_metadata()
    else:
        memories = tuple(recall_result)
        failures = ()
        metadata = {
            "raw_recall_degraded": False,
            "raw_recall_failure_count": 0,
        }
    results = [
        _raw_memory_search_result(
            memory,
            organization_id=organization_id,
            include_content=include_content,
            content_max_chars=content_max_chars,
        )
        for memory in memories
    ]
    return RawMemorySearchFetch(
        results=results,
        failures=failures,
        metadata=metadata,
    )


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
    content_max_chars: int = DEFAULT_SEARCH_CONTENT_MAX_CHARS,
    include_documents: bool = True,
    include_graph: bool = True,
    include_raw_memory: bool = True,
    memory_scope: str = "private",
    scope_key: str | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    use_enhanced: bool = True,
    boost_recent: bool = True,
    temporal_decay_days: float | None = None,
    reference_time: str | datetime | None = None,
    as_of: str | datetime | None = None,
    organization_id: str | None = None,
    principal_id: str | None = None,
    allowed_memory_scope_keys: set[str] | None = None,
    record_exposure: bool = True,
    include_retrieval_diagnostics: bool = False,
) -> SearchResponse:
    """Unified semantic search across knowledge graph AND documentation.

    Searches both Sibyl's knowledge graph (patterns, rules, episodes, tasks)
    AND crawled documentation (Surreal-backed vector search). Results are
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
        include_content: Include content in results (default True).
        content_max_chars: Maximum content characters per result (default 500).
        include_documents: Include crawled documentation in search (default True).
        include_graph: Include knowledge graph entities in search (default True).
        include_raw_memory: Include raw memory captures in search (default True).
        use_enhanced: Use enhanced hybrid retrieval for graph (default True).
        boost_recent: Apply temporal boosting for graph results (default True).
        reference_time: Optional query as-of timestamp for temporal ranking.
        as_of: Optional point-in-time filter for graph validity windows.

    Returns:
        SearchResponse with ranked results from both sources, including
        graph_count and document_count for result breakdown.

    EXAMPLES:
        search("error handling patterns", types=["pattern"], language="python")
        search("Next.js routing", source_name="next-dynenv")
        search("", types=["task"], status="todo", project="proj_auth")
    """
    search_started_at = time.perf_counter()
    stage_timings_ms = {
        "planning": 0.0,
        "graph_search": 0.0,
        "document_search": 0.0,
        "raw_memory_search": 0.0,
        "fusion": 0.0,
        "exposure": 0.0,
        "total": 0.0,
    }
    planning_started_at = time.perf_counter()

    # Clamp limit and offset
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    content_max_chars = max(0, min(content_max_chars, MAX_SEARCH_CONTENT_MAX_CHARS))

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
        include_raw_memory=include_raw_memory,
        content_max_chars=content_max_chars,
        limit=limit,
    )

    filters = {}
    if include_content:
        filters["content_max_chars"] = content_max_chars
    source_failures: list[CandidateSourceFailure] = []
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
    if include_raw_memory:
        filters["include_raw_memory"] = include_raw_memory
        filters["memory_scope"] = memory_scope
    if scope_key:
        filters["scope_key"] = scope_key
    if participants:
        filters["participants"] = list(participants)
    if labels:
        filters["labels"] = list(labels)
    if thread_id:
        filters["thread_id"] = thread_id
    if occurred_after:
        filters["occurred_after"] = str(occurred_after)
    if occurred_before:
        filters["occurred_before"] = str(occurred_before)
    if assignee:
        filters["assignee"] = assignee
    if since:
        filters["since"] = since
    resolved_reference_time = parse_temporal_datetime(reference_time)
    if reference_time:
        filters["reference_time"] = str(reference_time)
    resolved_as_of = _normalize_temporal_datetime(as_of)
    if as_of:
        filters["as_of"] = str(as_of)

    # Determine if we should search documents based on types filter
    search_documents = include_documents
    search_graph = include_graph
    search_raw_memory = bool(include_raw_memory and organization_id and principal_id and query)
    explicit_document_request = bool(source_id or source_name)
    explicit_raw_memory_request = bool(
        scope_key
        or participants
        or labels
        or thread_id
        or occurred_after
        or occurred_before
        or memory_scope != "private"
    )
    if types:
        # If 'document' is in types, search documents
        # If only 'document' is in types, skip graph search
        type_set = {t.lower() for t in types}
        if "document" in type_set:
            explicit_document_request = True
            search_documents = include_documents
            if type_set == {"document"}:
                search_graph = False
                search_raw_memory = False
        if "raw_memory" in type_set:
            explicit_raw_memory_request = True
            search_raw_memory = bool(include_raw_memory and organization_id and principal_id)
            if type_set == {"raw_memory"}:
                search_graph = False
                search_documents = False
        elif not explicit_raw_memory_request:
            search_raw_memory = False
        if "document" not in type_set and (source_id or source_name):
            # If source filters are set but document not in types, add document search
            search_documents = include_documents
        elif "document" not in type_set:
            # Types specified but document not included - skip document search
            search_documents = False

    if search_raw_memory and allowed_memory_scope_keys is not None:
        effective_scope_key = (
            principal_id if memory_scope == "private" and not scope_key else scope_key
        )
        if (
            memory_scope_policy_key(memory_scope, effective_scope_key)
            not in allowed_memory_scope_keys
        ):
            search_raw_memory = False

    if (
        search_documents
        and search_graph
        and not explicit_document_request
        and _has_graph_only_filters(
            category=category,
            status=status,
            project=project,
            source=source,
            assignee=assignee,
            since=since,
        )
    ):
        search_documents = False

    graph_results: list[SearchResult] = []
    doc_results: list[SearchResult] = []
    raw_memory_results: list[SearchResult] = []
    document_search_task: asyncio.Task[list[SearchResult]] | None = None
    raw_memory_search_task: asyncio.Task[RawMemorySearchFetch] | None = None
    if search_documents and query and organization_id:
        document_timeout_seconds = (
            DOCUMENT_SEARCH_GRAPH_JOIN_TIMEOUT_SECONDS
            if search_graph
            else DOCUMENT_SEARCH_TIMEOUT_SECONDS
        )
        document_search_task = asyncio.create_task(
            _timed_search_source(
                with_timeout(
                    _search_documents(
                        query=query,
                        organization_id=organization_id,
                        source_id=source_id,
                        source_name=source_name,
                        language=language,
                        limit=limit,
                        include_content=include_content,
                        content_max_chars=content_max_chars,
                    ),
                    timeout_seconds=document_timeout_seconds,
                    operation_name="document_search",
                ),
                timings=stage_timings_ms,
                stage="document_search",
            )
        )

    if search_raw_memory and query and organization_id and principal_id:
        raw_memory_search_task = asyncio.create_task(
            _timed_search_source(
                with_timeout(
                    _search_raw_memories(
                        query=query,
                        organization_id=organization_id,
                        principal_id=principal_id,
                        memory_scope=memory_scope,
                        scope_key=scope_key,
                        project_id=project,
                        source_id=source_id,
                        participants=participants,
                        labels=labels,
                        thread_id=thread_id,
                        occurred_after=occurred_after,
                        occurred_before=occurred_before,
                        as_of=resolved_as_of,
                        limit=limit,
                        include_content=include_content,
                        content_max_chars=content_max_chars,
                    ),
                    timeout_seconds=TIMEOUTS["search"],
                    operation_name="raw_memory_search",
                ),
                timings=stage_timings_ms,
                stage="raw_memory_search",
            )
        )

    requested_graph_types: set[str] = set()
    entity_types = None
    if types:
        entity_types = []
        for t in types:
            normalized_type = t.lower()
            if (
                normalized_type in VALID_ENTITY_TYPES or normalized_type == "guide"
            ) and normalized_type != "document":
                entity_type = EntityType(normalized_type)
                requested_graph_types.add(entity_type.value)
                entity_types.append(entity_type)

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
    stage_timings_ms["planning"] = (time.perf_counter() - planning_started_at) * 1000.0

    # =========================================================================
    # GRAPH SEARCH - Search knowledge graph entities
    # =========================================================================
    graph_started_at = time.perf_counter()
    graph_retrieval_metadata: dict[str, Any] = {}
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

            def graph_result_allowed(entity: Any) -> bool:
                return _matches_graph_filters(
                    entity,
                    language=language,
                    category=category,
                    status=status,
                    project=project,
                    principal_id=principal_id,
                    allowed_memory_scope_keys=allowed_memory_scope_keys,
                    source=source,
                    assignee=assignee,
                    since_date=since_date,
                    as_of=resolved_as_of,
                    accessible_projects=accessible_projects,
                )

            # Perform search - try enhanced hybrid first, then fall back to
            # the entity manager's direct search path.
            raw_results: list[tuple[Any, float]] = []
            enhanced_search_exhausted = False
            graph_search_failed = False

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
                        reference_time=resolved_reference_time,
                    )

                    hybrid_result = await with_timeout(
                        hybrid_search(
                            query=query,
                            client=client,
                            entity_manager=entity_manager,
                            entity_types=entity_types,
                            limit=limit * 3,
                            config=hybrid_config,
                            include_metadata=include_retrieval_diagnostics,
                            group_id=organization_id,
                            result_filter=graph_result_allowed,
                        ),
                        timeout_seconds=TIMEOUTS["search"],
                        operation_name="hybrid_search",
                    )
                    raw_results = hybrid_result.results
                    graph_retrieval_metadata = {
                        key: value
                        for key, value in hybrid_result.metadata.items()
                        if key != "query"
                    }
                    enhanced_search_exhausted = bool(
                        hybrid_result.metadata.get("entity_manager_search_completed")
                    )
                    log.debug("graph_search_enhanced", results=len(raw_results))

                except Exception as e:
                    log.warning("enhanced_search_failed_fallback", error_type=type(e).__name__)
                    _record_source_failure(
                        source_failures,
                        source="graph_enhanced",
                        error=e,
                    )

            # Fall back to direct entity-manager search
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
                        raw_results = temporal_boost(
                            raw_results,
                            decay_days=decay,
                            reference_time=resolved_reference_time,
                        )
                except Exception as e:
                    log.warning("fallback_graph_search_failed", error_type=type(e).__name__)
                    _record_source_failure(source_failures, source="graph", error=e)
                    graph_search_failed = True
                    raw_results = []

            if (
                query
                and not graph_search_failed
                and not enhanced_search_exhausted
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
                    _record_source_failure(
                        source_failures,
                        source="graph_exact_name",
                        error=e,
                    )

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
                    as_of=resolved_as_of,
                    accessible_projects=accessible_projects,
                    principal_id=principal_id,
                    allowed_memory_scope_keys=allowed_memory_scope_keys,
                )

            raw_results = [
                (entity, score) for entity, score in raw_results if graph_result_allowed(entity)
            ]

            if requested_graph_types:
                typed_results = [
                    (entity, score)
                    for entity, score in raw_results
                    if _matches_requested_graph_type(entity, requested_graph_types)
                ]
            else:
                typed_results = raw_results

            if query and not typed_results and requested_graph_types and not graph_search_failed:
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
                    _record_source_failure(source_failures, source="graph", error=e)
                    fallback_results = []
                typed_results = [
                    (entity, score)
                    for entity, score in fallback_results
                    if _matches_requested_graph_type(entity, requested_graph_types)
                    and graph_result_allowed(entity)
                ]

            raw_results = typed_results

            # Filter and convert to SearchResult
            for entity, score in raw_results:
                if not graph_result_allowed(entity):
                    continue

                graph_results.append(
                    graph_entity_to_search_result(
                        entity,
                        organization_id=organization_id,
                        principal_id=principal_id,
                        score=score,
                        include_content=include_content,
                        content_max_chars=content_max_chars,
                    )
                )

                if len(graph_results) >= offset + limit:
                    break

        except Exception as e:
            log.warning("graph_search_failed", error_type=type(e).__name__)
            _record_source_failure(source_failures, source="graph", error=e)
    stage_timings_ms["graph_search"] = (time.perf_counter() - graph_started_at) * 1000.0
    if graph_retrieval_metadata:
        filters["graph_retrieval"] = graph_retrieval_metadata

    # =========================================================================
    # DOCUMENT SEARCH - Search crawled documentation
    # =========================================================================
    if document_search_task is not None:
        try:
            doc_results = await document_search_task
            if organization_id:
                for result in doc_results:
                    result.metadata = _document_candidate_metadata(
                        result,
                        organization_id=organization_id,
                    )
            log.debug("document_search_complete", results=len(doc_results))
        except Exception as e:
            log.warning("document_search_failed", error_type=type(e).__name__)
            _record_source_failure(source_failures, source="document", error=e)

    if raw_memory_search_task is not None:
        try:
            raw_memory_fetch = await raw_memory_search_task
            raw_memory_results = raw_memory_fetch.results
            source_failures.extend(raw_memory_fetch.failures)
            filters.update(raw_memory_fetch.metadata)
            log.debug("raw_memory_search_complete", results=len(raw_memory_results))
        except Exception as e:
            log.warning("raw_memory_search_failed", error_type=type(e).__name__)
            _record_source_failure(source_failures, source="raw_memory", error=e)

    # =========================================================================
    # MERGE AND RANK RESULTS
    # =========================================================================
    fusion_started_at = time.perf_counter()
    # Deduplicate by ID, keeping highest score for each entity
    seen_ids: dict[str, SearchResult] = {}
    for result in graph_results + doc_results + raw_memory_results:
        if result.id not in seen_ids or result.score > seen_ids[result.id].score:
            seen_ids[result.id] = result

    deduped_graph_results = [
        result for result in graph_results if seen_ids.get(result.id) is result
    ]
    deduped_doc_results = [result for result in doc_results if seen_ids.get(result.id) is result]
    deduped_raw_memory_results = [
        result for result in raw_memory_results if seen_ids.get(result.id) is result
    ]

    all_results = _rank_fuse_search_results(
        deduped_graph_results,
        deduped_doc_results,
        deduped_raw_memory_results,
    )

    # Apply pagination
    total_count = len(all_results)
    paginated_results = all_results[offset : offset + limit]
    has_more = offset + len(paginated_results) < total_count
    _add_source_failure_filters(filters, source_failures)
    stage_timings_ms["fusion"] = (time.perf_counter() - fusion_started_at) * 1000.0

    exposure_started_at = time.perf_counter()
    if record_exposure and paginated_results and organization_id:
        try:
            filters[_USAGE_EXPOSURE_SUMMARY_KEY] = await with_timeout(
                annotate_search_result_exposures(
                    paginated_results,
                    organization_id=organization_id,
                    principal_id=principal_id,
                    project_id=project,
                    source_surface="search",
                    request_metadata={
                        "query": query,
                        "types": types,
                        "limit": limit,
                        "offset": offset,
                        "project": project,
                        "source_id": source_id,
                        "source_name": source_name,
                        "memory_scope": memory_scope,
                        "scope_key": scope_key,
                        "result_ids": [result.id for result in paginated_results],
                    },
                ),
                timeout_seconds=TIMEOUTS["search"],
                operation_name="usage_exposure_annotation",
            )
        except Exception as exc:
            log.warning(
                "usage_exposure_annotation_failed",
                error_type=type(exc).__name__,
                exc_info=True,
            )
    stage_timings_ms["exposure"] = (time.perf_counter() - exposure_started_at) * 1000.0
    stage_timings_ms["total"] = (time.perf_counter() - search_started_at) * 1000.0
    filters["stage_timings_ms"] = stage_timings_ms

    return SearchResponse(
        results=paginated_results,
        total=total_count,
        query=query,
        filters=filters,
        graph_count=len([r for r in paginated_results if r.result_origin == "graph"]),
        document_count=len([r for r in paginated_results if r.result_origin == "document"]),
        raw_memory_count=len([r for r in paginated_results if r.result_origin == "raw_memory"]),
        limit=limit,
        offset=offset,
        has_more=has_more,
    )
