"""Raw memory recall, fusion, access tracking, and review selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import structlog

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.fulltext import (
    build_fulltext_terms,
    build_match_disjunction,
)
from sibyl_core.backends.surreal.knn import knn_search_effort
from sibyl_core.config import settings
from sibyl_core.embeddings.providers import (
    EmbeddingProvider,
)
from sibyl_core.memory_pipeline.retrieval import CandidateSourceResult
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services import content_client
from sibyl_core.services import content_documents as documents
from sibyl_core.services import content_models as models
from sibyl_core.services.content_models import RawMemory, RawMemoryRecallResult
from sibyl_core.utils.resilience import with_timeout

_REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES = frozenset(
    {
        "reflection",
        "reflection_candidate",
        "reflection_source",
        "synthesis_artifact",
    }
)

_EMBEDDED_SURREAL_SCHEMES = ("memory://", "surrealkv://", "rocksdb://", "file://")

log = structlog.get_logger()

_RAW_MEMORY_RECALL_FIELDS = ", ".join(
    (
        "id AS record_id",
        "uuid",
        "organization_id",
        "source_id",
        "principal_id",
        "memory_scope",
        "scope_key",
        "agent_id",
        "project_id",
        "review_state",
        "entity_id",
        "entity_type",
        "title",
        "raw_content",
        "tags",
        "metadata",
        "provenance",
        "capture_surface",
        "created_by_user_id",
        "captured_at",
        "deleted_at",
        "purge_after",
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
        "misled_count",
        "created_at",
    )
)


@dataclass(frozen=True, slots=True)
class _RawMemoryRecallFilters:
    source_ids: tuple[str, ...] = ()
    participants: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    thread_id: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None
    as_of: datetime | None = None
    as_of_text: str | None = None


def _memory_scope_where(
    *,
    organization_id: str,
    principal_id: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
    agent_id: str | None = None,
    project_id: str | None = None,
) -> tuple[str, dict[str, object]]:
    models.validate_raw_memory_scope(memory_scope, scope_key)
    clauses = [
        "organization_id = $organization_id",
        "memory_scope = $memory_scope",
    ]
    params: dict[str, object] = {
        "organization_id": organization_id,
        "memory_scope": memory_scope.value,
    }
    if memory_scope is MemoryScope.PRIVATE:
        clauses.append("principal_id = $principal_id")
        params["principal_id"] = principal_id
    elif scope_key is not None:
        clauses.append("scope_key = $scope_key")
        params["scope_key"] = scope_key
    if agent_id:
        clauses.append("agent_id = $agent_id")
        params["agent_id"] = agent_id
    else:
        clauses.append("(capture_surface != $agent_diary_surface OR capture_surface = NONE)")
        params["agent_diary_surface"] = models.AGENT_DIARY_CAPTURE_SURFACE
    if project_id:
        clauses.append("project_id = $project_id")
        params["project_id"] = project_id
    return " AND ".join(clauses), params


def _surreal_type_is_string(field: str) -> str:
    if settings.resolved_surreal_url.startswith(_EMBEDDED_SURREAL_SCHEMES):
        return f"type::is::string({field})"
    return f"type::is_string({field})"


def _surreal_type_is_datetime(field: str) -> str:
    if settings.resolved_surreal_url.startswith(_EMBEDDED_SURREAL_SCHEMES):
        return f"type::is::datetime({field})"
    return f"type::is_datetime({field})"


def _raw_memory_recall_where(
    *,
    organization_id: str,
    principal_id: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
    agent_id: str | None = None,
    project_id: str | None = None,
    filters: _RawMemoryRecallFilters | None = None,
) -> tuple[str, dict[str, object]]:
    where_clause, params = _memory_scope_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
    )
    clauses = [where_clause]
    filters = filters or _RawMemoryRecallFilters()
    if filters.source_ids:
        clauses.append("source_id IN $source_ids")
        params["source_ids"] = list(filters.source_ids)
    if filters.participants:
        clauses.append("metadata.participants CONTAINSANY $participants")
        params["participants"] = list(filters.participants)
    if filters.labels:
        clauses.append("(tags CONTAINSANY $labels OR metadata.labels CONTAINSANY $labels)")
        params["labels"] = list(filters.labels)
    if filters.thread_id:
        clauses.append(
            "(metadata.thread_id = $thread_id "
            "OR metadata.source_record_metadata.thread_id = $thread_id)"
        )
        params["thread_id"] = filters.thread_id
    if filters.occurred_after:
        clauses.append("metadata.occurred_at >= $occurred_after")
        params["occurred_after"] = filters.occurred_after
    if filters.occurred_before:
        clauses.append("metadata.occurred_at <= $occurred_before")
        params["occurred_before"] = filters.occurred_before
    if filters.as_of:
        created_at_is_string = _surreal_type_is_string("created_at")
        created_at_is_datetime = _surreal_type_is_datetime("created_at")
        captured_at_is_string = _surreal_type_is_string("captured_at")
        captured_at_is_datetime = _surreal_type_is_datetime("captured_at")
        valid_at_is_string = _surreal_type_is_string("metadata.valid_at")
        valid_at_is_datetime = _surreal_type_is_datetime("metadata.valid_at")
        valid_from_is_string = _surreal_type_is_string("metadata.valid_from")
        valid_from_is_datetime = _surreal_type_is_datetime("metadata.valid_from")
        invalid_at_is_string = _surreal_type_is_string("metadata.invalid_at")
        invalid_at_is_datetime = _surreal_type_is_datetime("metadata.invalid_at")
        valid_to_is_string = _surreal_type_is_string("metadata.valid_to")
        valid_to_is_datetime = _surreal_type_is_datetime("metadata.valid_to")
        clauses.extend(
            [
                "(created_at = NONE "
                f"OR ({created_at_is_datetime} AND created_at <= $as_of) "
                f"OR ({created_at_is_string} AND created_at <= $as_of_text))",
                "(captured_at = NONE "
                f"OR ({captured_at_is_datetime} AND captured_at <= $as_of) "
                f"OR ({captured_at_is_string} AND captured_at <= $as_of_text))",
                "(metadata.valid_at = NONE "
                f"OR ({valid_at_is_datetime} AND metadata.valid_at <= $as_of) "
                f"OR ({valid_at_is_string} AND metadata.valid_at <= $as_of_text))",
                "(metadata.valid_from = NONE "
                f"OR ({valid_from_is_datetime} AND metadata.valid_from <= $as_of) "
                f"OR ({valid_from_is_string} AND metadata.valid_from <= $as_of_text))",
                "(metadata.invalid_at = NONE "
                f"OR ({invalid_at_is_datetime} AND metadata.invalid_at > $as_of) "
                f"OR ({invalid_at_is_string} AND metadata.invalid_at > $as_of_text))",
                "(metadata.valid_to = NONE "
                f"OR ({valid_to_is_datetime} AND metadata.valid_to > $as_of) "
                f"OR ({valid_to_is_string} AND metadata.valid_to > $as_of_text))",
            ]
        )
        params["as_of"] = filters.as_of
        params["as_of_text"] = filters.as_of_text or filters.as_of.isoformat()
    return " AND ".join(clauses), params


async def _recall_raw_memory_lexical(
    client: SurrealContentClient,
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
    agent_id: str | None,
    project_id: str | None,
    filters: _RawMemoryRecallFilters | None = None,
    as_of: datetime | None = None,
    limit: int,
) -> list[RawMemory]:
    where_clause, params = _raw_memory_recall_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        filters=filters,
    )
    rows = await content_client.select_many(
        client,
        f"SELECT {_RAW_MEMORY_RECALL_FIELDS} FROM raw_captures "
        f"WHERE {where_clause} ORDER BY captured_at DESC LIMIT $limit;",
        **params,
        limit=max(limit * 4, limit),
    )
    scored: list[RawMemory] = []
    for row in rows:
        memory = models.raw_memory_from_record(row)
        memory.score = documents.lexical_score(query, memory.title, memory.raw_content)
        if (
            memory.score > 0
            and models.raw_memory_recallable(memory)
            and models.raw_memory_matches_as_of(memory, as_of)
        ):
            scored.append(memory)
    return sorted(scored, key=lambda memory: (-memory.score, memory.captured_at or datetime.min))[
        :limit
    ]


async def _recall_raw_memory_fulltext(
    client: SurrealContentClient,
    *,
    where_clause: str,
    params: Mapping[str, object],
    query: str,
    as_of: datetime | None,
    limit: int,
) -> list[RawMemory]:
    match = build_match_disjunction(["title", "raw_content"], build_fulltext_terms(query))
    if match is None:
        return []
        # Highlights reference one match operator each; pin them to the first
        # term's operator per field, so snippets mark the leading salient term.
    term_count = len(match.params)
    rows = await with_timeout(
        content_client.select_many_raw(
            client,
            f"SELECT {_RAW_MEMORY_RECALL_FIELDS}, "
            f"{match.score_expr} AS score, "
            "search::highlight('<mark>', '</mark>', 0) AS title_snippet, "
            f"search::highlight('<mark>', '</mark>', {term_count}) AS content_snippet "
            f"FROM raw_captures WHERE {where_clause} "
            f"AND {match.where_clause} "
            "ORDER BY score DESC, captured_at DESC LIMIT $limit;",
            **params,
            **match.params,
            limit=limit * content_client.LIFECYCLE_FILTER_OVERFETCH_FACTOR,
        ),
        timeout_seconds=content_client.DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS,
        operation_name="surreal_raw_memory_fulltext_recall",
    )
    return models.recallable_memories(
        [models.raw_memory_from_record(row) for row in rows],
        limit=limit,
        as_of=as_of,
    )


async def _recall_raw_memory_vector(
    client: SurrealContentClient,
    *,
    where_clause: str,
    params: Mapping[str, object],
    query_embedding: list[float],
    as_of: datetime | None,
    limit: int,
) -> list[RawMemory]:
    candidate_limit = max(limit * content_client.LIFECYCLE_FILTER_OVERFETCH_FACTOR, limit)
    knn_effort = knn_search_effort(candidate_limit, content_client.CONTENT_KNN_EF_FLOOR)
    rows = await with_timeout(
        content_client.select_many_raw(
            client,
            "SELECT * FROM ("
            f"SELECT {_RAW_MEMORY_RECALL_FIELDS}, "
            "(1 - vector::distance::knn()) AS score "
            f"FROM raw_captures WHERE {where_clause} "
            f"AND embedding <|{candidate_limit}, {knn_effort}|> $query_embedding"
            ") ORDER BY score DESC, captured_at DESC LIMIT $candidate_limit;",
            **params,
            query_embedding=query_embedding,
            candidate_limit=candidate_limit,
        ),
        timeout_seconds=content_client.DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS,
        operation_name="surreal_raw_memory_vector_recall",
    )
    return models.recallable_memories(
        [models.raw_memory_from_record(row) for row in rows],
        limit=limit,
        as_of=as_of,
    )


async def raw_memory_query_embedding(query: str) -> list[float] | None:
    provider: EmbeddingProvider | None = None
    try:
        provider = models.configured_raw_memory_embedding_provider()
        if provider is None:
            return None
        embeddings = await provider.embed_texts([query], input_kind="query")
        return models.embedding_vector_from_batch(embeddings, provider.metadata.dimensions)
    except Exception as exc:
        metadata = provider.metadata if provider is not None else None
        log.warning(
            "raw_memory_query_embedding_failed",
            provider=metadata.provider if metadata is not None else None,
            model=metadata.model if metadata is not None else None,
            dimensions=metadata.dimensions if metadata is not None else None,
            query_length=len(query),
            error_type=type(exc).__name__,
        )
        return None


def _python_raw_memory_rrf_scores(
    result_lists: Sequence[Sequence[RawMemory]],
    *,
    k: float = 60.0,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for memories in result_lists:
        for rank, memory in enumerate(memories, start=1):
            scores[memory.id] = scores.get(memory.id, 0.0) + (1.0 / (k + rank))
    return scores


async def _surreal_raw_memory_rrf_scores(
    client: SurrealContentClient,
    result_lists: Sequence[Sequence[RawMemory]],
    *,
    limit: int,
    k: float = 60.0,
) -> dict[str, float]:
    rrf_inputs = [
        [{"id": memory.id, "score": memory.score} for memory in memories]
        for memories in result_lists
    ]
    if not any(rrf_inputs):
        return {}
    unique_count = len({memory.id for memories in result_lists for memory in memories})
    try:
        result = await client.execute_query(
            "RETURN search::rrf($lists, $limit, $k);",
            lists=rrf_inputs,
            limit=max(int(limit), unique_count, 1),
            k=k,
        )
    except Exception:
        return {}
    if content_client.query_error(result) is not None:
        return {}

    scores: dict[str, float] = {}
    for row in models.normalize_records_preserving_id(result):
        memory_id = models.coerce_optional_str(
            row.get("id") or row.get("uuid") or row.get("record_id")
        )
        raw_score = row.get("rrf_score", row.get("rff_score", row.get("fuse_score")))
        if memory_id and isinstance(raw_score, int | float):
            scores[memory_id] = float(raw_score)
    return scores


async def _fuse_raw_memory_results(
    client: SurrealContentClient,
    result_lists: Sequence[Sequence[RawMemory]],
    *,
    limit: int,
) -> list[RawMemory]:
    raw_lists = [list(results) for results in result_lists if results]
    if not raw_lists:
        return []
    if len(raw_lists) == 1:
        return raw_lists[0][:limit]

    memory_by_id: dict[str, RawMemory] = {}
    first_seen: dict[str, tuple[int, int]] = {}
    for list_index, memories in enumerate(raw_lists):
        for rank, memory in enumerate(memories, start=1):
            memory_by_id.setdefault(memory.id, memory)
            first_seen.setdefault(memory.id, (list_index, rank))

    scores = await _surreal_raw_memory_rrf_scores(client, raw_lists, limit=limit)
    if set(scores) != set(memory_by_id):
        fallback_scores = _python_raw_memory_rrf_scores(raw_lists)
        for memory_id, score in fallback_scores.items():
            scores.setdefault(memory_id, score)

    fused: list[RawMemory] = []
    ranked_ids = sorted(
        memory_by_id,
        key=lambda memory_id: (-scores.get(memory_id, 0.0), first_seen[memory_id]),
    )
    for memory_id in ranked_ids[:limit]:
        memory = memory_by_id[memory_id]
        score = scores.get(memory_id, 0.0)
        memory.score = score
        fused.append(memory)
    return fused


def _raw_recall_filters(
    *,
    source_ids: Sequence[str] | None,
    participants: Sequence[str] | None,
    labels: Sequence[str] | None,
    thread_id: str | None,
    occurred_after: datetime | str | None,
    occurred_before: datetime | str | None,
    as_of: datetime | str | None,
) -> _RawMemoryRecallFilters:
    as_of_datetime = _as_of_filter_value(as_of)
    return _RawMemoryRecallFilters(
        source_ids=tuple(_normalized_filter_values(source_ids)),
        participants=tuple(_normalized_filter_values(participants)),
        labels=tuple(_normalized_filter_values(labels)),
        thread_id=models.coerce_optional_str(thread_id),
        occurred_after=_datetime_filter_value(occurred_after),
        occurred_before=_datetime_filter_value(occurred_before),
        as_of=as_of_datetime,
        as_of_text=as_of_datetime.isoformat() if as_of_datetime else None,
    )


def _normalized_filter_values(values: Sequence[str] | None) -> list[str]:
    if values is None:
        return []
    return [value for item in values if (value := str(item).strip())]


def _datetime_filter_value(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _as_of_filter_value(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    parsed = models.normalize_raw_temporal_datetime(value)
    return parsed


async def _recall_raw_memory_result(
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    source_ids: Sequence[str] | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    as_of: datetime | str | None = None,
    limit: int = 10,
    raise_on_source_failure: bool,
) -> RawMemoryRecallResult:
    normalized_query = query.strip()
    if not normalized_query or limit <= 0:
        return RawMemoryRecallResult(())

    normalized_scope = models.coerce_memory_scope(memory_scope)
    filters = _raw_recall_filters(
        source_ids=source_ids,
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        as_of=as_of,
    )
    effective_as_of = filters.as_of or datetime.now(UTC)
    filters = replace(
        filters,
        as_of=effective_as_of,
        as_of_text=filters.as_of_text or effective_as_of.isoformat(),
    )
    where_clause, params = _raw_memory_recall_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=normalized_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        filters=filters,
    )
    source_results: list[CandidateSourceResult[RawMemory]] = []
    query_embedding: list[float] | None = None
    try:
        query_embedding = await raw_memory_query_embedding(normalized_query)
    except Exception as exc:
        source_results.append(CandidateSourceResult.failed("raw_vector", type(exc).__name__))
    async with content_client.surreal_content_client() as client:
        fulltext_memories: list[RawMemory] = []
        vector_memories: list[RawMemory] = []
        try:
            fulltext_memories = await _recall_raw_memory_fulltext(
                client,
                where_clause=where_clause,
                params=params,
                query=normalized_query,
                as_of=effective_as_of,
                limit=limit,
            )
        except Exception as exc:
            log.warning(
                "raw_memory_fulltext_recall_failed",
                organization_id=organization_id,
                memory_scope=normalized_scope.value,
                has_scope_key=scope_key is not None,
                error_type=type(exc).__name__,
            )
            fulltext_memories = []
            source_results.append(CandidateSourceResult.failed("raw_fulltext", type(exc).__name__))
        else:
            source_results.append(CandidateSourceResult.success("raw_fulltext", fulltext_memories))
        if query_embedding is not None:
            try:
                vector_memories = await _recall_raw_memory_vector(
                    client,
                    where_clause=where_clause,
                    params=params,
                    query_embedding=query_embedding,
                    as_of=effective_as_of,
                    limit=limit,
                )
            except Exception as exc:
                log.warning(
                    "raw_memory_vector_recall_failed",
                    organization_id=organization_id,
                    memory_scope=normalized_scope.value,
                    has_scope_key=scope_key is not None,
                    error_type=type(exc).__name__,
                )
                vector_memories = []
                source_results.append(
                    CandidateSourceResult.failed("raw_vector", type(exc).__name__)
                )
            else:
                source_results.append(CandidateSourceResult.success("raw_vector", vector_memories))
        memories = await _fuse_raw_memory_results(
            client,
            [fulltext_memories, vector_memories],
            limit=limit,
        )
        if memories:
            return RawMemoryRecallResult(tuple(memories), tuple(source_results))
        try:
            lexical_memories = await _recall_raw_memory_lexical(
                client,
                organization_id=organization_id,
                principal_id=principal_id,
                query=normalized_query,
                memory_scope=normalized_scope,
                scope_key=scope_key,
                agent_id=agent_id,
                project_id=project_id,
                filters=filters,
                as_of=effective_as_of,
                limit=limit,
            )
        except (RuntimeError, TimeoutError) as exc:
            source_results.append(CandidateSourceResult.failed("raw_lexical", type(exc).__name__))
            if raise_on_source_failure:
                raise
            lexical_memories = []
        else:
            source_results.append(CandidateSourceResult.success("raw_lexical", lexical_memories))
        return RawMemoryRecallResult(tuple(lexical_memories), tuple(source_results))


async def recall_raw_memory_with_sources(
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    source_ids: Sequence[str] | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    as_of: datetime | str | None = None,
    limit: int = 10,
) -> RawMemoryRecallResult:
    return await _recall_raw_memory_result(
        organization_id=organization_id,
        principal_id=principal_id,
        query=query,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        source_ids=source_ids,
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        as_of=as_of,
        limit=limit,
        raise_on_source_failure=False,
    )


async def recall_raw_memory(
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    source_ids: Sequence[str] | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    as_of: datetime | str | None = None,
    limit: int = 10,
) -> list[RawMemory]:
    result = await _recall_raw_memory_result(
        organization_id=organization_id,
        principal_id=principal_id,
        query=query,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        source_ids=source_ids,
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        as_of=as_of,
        limit=limit,
        raise_on_source_failure=True,
    )
    return list(result.memories)


async def list_raw_memories_for_scope(
    *,
    organization_id: str,
    principal_id: str,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    include_lifecycle_hidden: bool = False,
) -> list[RawMemory]:
    if limit <= 0:
        return []
    normalized_scope = models.coerce_memory_scope(memory_scope)
    query_limit = (
        limit
        if include_lifecycle_hidden
        else limit * content_client.LIFECYCLE_FILTER_OVERFETCH_FACTOR
    )
    where_clause, params = _memory_scope_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=normalized_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
    )
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            f"SELECT * FROM raw_captures WHERE {where_clause} "
            "ORDER BY captured_at DESC LIMIT $limit;",
            **params,
            limit=query_limit,
        )
    memories = [models.raw_memory_from_record(row) for row in rows]
    if include_lifecycle_hidden:
        return memories[:limit]
    return models.recallable_memories(memories, limit=limit, as_of=datetime.now(UTC))


async def list_reflection_candidate_reviews(
    *,
    organization_id: str,
    review_state: str = "pending",
    limit: int = 50,
) -> list[RawMemory]:
    if limit <= 0:
        return []
    target_review_state = review_state.strip().lower()
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id "
            "AND capture_surface = $capture_surface "
            "AND review_state = $review_state "
            "ORDER BY captured_at ASC LIMIT $limit;",
            organization_id=organization_id,
            capture_surface="reflection_candidate",
            review_state=target_review_state,
            limit=limit,
        )
    memories = [models.raw_memory_from_record(row) for row in rows]
    memories = [
        memory
        for memory in memories
        if str(memory.review_state or "pending").strip().lower() == target_review_state
    ]
    memories = sorted(
        memories,
        key=lambda memory: (
            memory.captured_at or memory.created_at or datetime.min.replace(tzinfo=UTC)
        ),
    )
    return memories[:limit]


async def list_reflection_dream_source_memories(
    *,
    organization_id: str,
    limit: int = 50,
) -> list[RawMemory]:
    if limit <= 0:
        return []
    query_limit = limit * content_client.LIFECYCLE_FILTER_OVERFETCH_FACTOR
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id "
            "AND (capture_surface != $candidate_surface OR capture_surface = NONE) "
            "AND (capture_surface != $source_surface OR capture_surface = NONE) "
            "AND (capture_surface != $reflection_surface OR capture_surface = NONE) "
            "AND (capture_surface != $synthesis_surface OR capture_surface = NONE) "
            "ORDER BY captured_at ASC LIMIT $limit;",
            organization_id=organization_id,
            candidate_surface="reflection_candidate",
            source_surface="reflection_source",
            reflection_surface="reflection",
            synthesis_surface="synthesis_artifact",
            limit=query_limit,
        )
    memories = [models.raw_memory_from_record(row) for row in rows]
    return [
        memory
        for memory in memories
        if models.raw_memory_currently_recallable(memory)
        and models.raw_memory_capture_surface(memory)
        not in _REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES
        and not memory.metadata.get("reflection_dream_processed_at")
    ][:limit]
