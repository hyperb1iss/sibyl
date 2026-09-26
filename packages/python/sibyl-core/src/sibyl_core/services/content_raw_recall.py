"""Raw memory recall, fusion, access tracking, and review selection."""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

import structlog

from sibyl_core.ai.bedrock import BEDROCK_GEO_PREFIXES, is_arn
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.fulltext import (
    build_fulltext_terms,
    build_match_disjunction,
)
from sibyl_core.backends.surreal.knn import KNN_TYPE_OVERFETCH_CAP, knn_search_effort
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
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
from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
from sibyl_core.utils.resilience import with_timeout

_REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES = frozenset(
    {
        "reflection",
        "reflection_candidate",
        "reflection_source",
        "synthesis_artifact",
    }
)

#: How many times deeper than the requested neighbours the dream job's HNSW
#: read walks. Pending checks reject covered, unauthorized and changed sources
#: after the read, and the exact rerank only orders what the pool holds, so the
#: pool runs deeper than the page to keep the nearest pending sources inside it.
DREAM_NEIGHBOUR_POOL_FACTOR = 4

log = structlog.get_logger()

# Reported by the raw_vector lane when the scope has captures but none holds a
# vector from the query's embedding model.
RAW_VECTOR_EMBEDDINGS_MISSING = "RawEmbeddingsMissing"
# Noted on an empty raw_vector result when the coverage walk hit its row cap
# before it could tell a scope without vectors from one with no matches.
RAW_VECTOR_COVERAGE_UNKNOWN = "embedding_coverage_unknown"

_RAW_MEMORY_RECALL_FIELDS = ", ".join(
    (
        "id AS record_id",
        "uuid",
        "revision",
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
class RawQueryEmbedding:
    """A query vector and the model that produced it.

    Cosine similarity between vectors from two models is noise, so the raw
    vector lane scores the vector only against captures stamped with the
    same space.
    """

    vector: list[float]
    space: models.RawEmbeddingSpace


def _raw_vector_model_key(space: models.RawEmbeddingSpace) -> str:
    """The expression a stored stamp's model is matched by.

    A Bedrock stamp may name its model through an inference-profile or
    foundation-model ARN, so it is matched by the last path segment, which is
    the model or profile ID the ARN ends in. Other providers' model names can
    contain slashes of their own, so they are matched whole.
    """
    if space.provider == "bedrock":
        return "array::last(string::split(metadata.embedding_metadata.model ?? '', '/'))"
    return "metadata.embedding_metadata.model"


def _raw_vector_model_keys(space: models.RawEmbeddingSpace) -> list[str]:
    """Every value ``_raw_vector_model_key`` yields for a stamp in this space.

    A scope-free Bedrock ID also matches under any geographic profile prefix,
    whether the stamp holds the prefixed ID or an ARN naming it. An opaque
    application-profile ARN matches only a stamp ending in the same profile ID.
    """
    if space.provider != "bedrock":
        return [space.model]
    if is_arn(space.model):
        return [space.model.rsplit("/", 1)[-1]]
    return [space.model, *(f"{prefix}.{space.model}" for prefix in BEDROCK_GEO_PREFIXES)]


def _raw_vector_space_params(space: models.RawEmbeddingSpace) -> dict[str, object]:
    return {
        "query_embedding_provider": space.provider,
        "query_embedding_models": _raw_vector_model_keys(space),
        "query_embedding_dimensions": space.dimensions,
    }


def _raw_vector_space_match(space: models.RawEmbeddingSpace) -> str:
    """A capture whose stamp names the query's model, as plain conjuncts.

    Plain conjuncts, with no grouping, are what embedded KNN can prefilter on.
    """
    return (
        "metadata.embedding_metadata.provider = $query_embedding_provider "
        f"AND {_raw_vector_model_key(space)} IN $query_embedding_models "
        "AND metadata.embedding_metadata.dimensions = $query_embedding_dimensions"
    )


def _raw_vector_space_mismatch(space: models.RawEmbeddingSpace) -> str:
    """A capture the query's vector cannot score: no vector, no stamp, or another model."""
    return _raw_memory_disjunction(
        "embedding = NONE",
        "metadata.embedding_metadata.provider != $query_embedding_provider",
        f"{_raw_vector_model_key(space)} NOT IN $query_embedding_models",
        "metadata.embedding_metadata.dimensions != $query_embedding_dimensions",
    )


@dataclass(frozen=True, slots=True)
class _RawMemoryRecallFilters:
    source_ids: tuple[str, ...] = ()
    capture_ids: tuple[str, ...] | None = None
    participants: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    thread_id: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None
    as_of: datetime | None = None
    as_of_text: str | None = None


def _raw_memory_disjunction(*branches: str) -> str:
    if is_embedded_surreal_url(settings.resolved_surreal_url):
        # Embedded KNN cannot prefilter grouped subqueries, including boolean
        # parentheses. Function arguments retain the same ungrouped predicates.
        return f"array::any([{', '.join(branches)}])"
    return "(" + " OR ".join(f"({b})" if " AND " in b else b for b in branches) + ")"


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
        clauses.append(
            _raw_memory_disjunction(
                "capture_surface != $agent_diary_surface", "capture_surface = NONE"
            )
        )
        params["agent_diary_surface"] = models.AGENT_DIARY_CAPTURE_SURFACE
    if project_id:
        clauses.append("project_id = $project_id")
        params["project_id"] = project_id
    return " AND ".join(clauses), params


def _surreal_type_is_string(field: str) -> str:
    if is_embedded_surreal_url(settings.resolved_surreal_url):
        return f"type::is::string({field})"
    return f"type::is_string({field})"


def _surreal_type_is_datetime(field: str) -> str:
    if is_embedded_surreal_url(settings.resolved_surreal_url):
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
    if filters.capture_ids is not None:
        clauses.append("uuid IN $capture_ids")
        params["capture_ids"] = list(filters.capture_ids)
    if filters.participants:
        clauses.append("metadata.participants CONTAINSANY $participants")
        params["participants"] = list(filters.participants)
    if filters.labels:
        clauses.append(
            _raw_memory_disjunction(
                "tags CONTAINSANY $labels", "metadata.labels CONTAINSANY $labels"
            )
        )
        params["labels"] = list(filters.labels)
    if filters.thread_id:
        clauses.append(
            _raw_memory_disjunction(
                "metadata.thread_id = $thread_id",
                "metadata.source_record_metadata.thread_id = $thread_id",
            )
        )
        params["thread_id"] = filters.thread_id
    if filters.occurred_after:
        clauses.append("metadata.occurred_at >= $occurred_after")
        params["occurred_after"] = filters.occurred_after
    if filters.occurred_before:
        clauses.append("metadata.occurred_at <= $occurred_before")
        params["occurred_before"] = filters.occurred_before
    if filters.as_of:
        for field, comparison in (
            ("created_at", "<="),
            ("captured_at", "<="),
            ("metadata.valid_at", "<="),
            ("metadata.valid_from", "<="),
            ("metadata.invalid_at", ">"),
            ("metadata.valid_to", ">"),
        ):
            clauses.append(
                _raw_memory_disjunction(
                    f"{field} = NONE",
                    f"{_surreal_type_is_datetime(field)} AND {field} {comparison} $as_of",
                    f"{_surreal_type_is_string(field)} AND {field} {comparison} $as_of_text",
                )
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
    query_embedding: RawQueryEmbedding,
    as_of: datetime | None,
    limit: int,
) -> list[RawMemory]:
    """Nearest captures whose vector comes from the query's embedding model.

    The model filter sits inside the HNSW bracket beside the scope filters,
    so membership, scope and model all precede the neighbour limit. Nearer
    vectors from a model the deployment switched away from therefore cannot
    fill the pool and crowd current captures out of it. When every capture
    shares the query's model, the predicate holds for every candidate and
    the walk goes no deeper. Captures it excludes remain reachable through
    the fulltext lane.

    There is no score floor. Once every scored vector shares the query's
    model the cosine is meaningful, and fusion ranks by position, not score.
    """
    candidate_limit = max(limit * content_client.LIFECYCLE_FILTER_OVERFETCH_FACTOR, limit)
    knn_effort = knn_search_effort(candidate_limit, content_client.CONTENT_KNN_EF_FLOOR)
    rows = await with_timeout(
        content_client.select_many_raw(
            client,
            "SELECT * FROM ("
            f"SELECT {_RAW_MEMORY_RECALL_FIELDS}, "
            "(1 - vector::distance::knn()) AS score "
            "FROM raw_captures WITH INDEX idx_raw_captures_embedding "
            f"WHERE {where_clause} "
            f"AND {_raw_vector_space_match(query_embedding.space)} "
            f"AND embedding <|{candidate_limit}, {knn_effort}|> $query_embedding"
            ") ORDER BY score DESC, captured_at DESC LIMIT $candidate_limit;",
            **params,
            **_raw_vector_space_params(query_embedding.space),
            query_embedding=query_embedding.vector,
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


# Rows the coverage probe will read per side before it stops judging. A scope
# with more ineligible rows than this on one side is reported as healthy, never
# as missing, so a truncated walk cannot raise a false report.
_COVERAGE_ROW_CAP = 512


async def _eligible_rows_present(
    client: SurrealContentClient,
    *,
    where_clause: str,
    params: Mapping[str, object],
    as_of: datetime | None,
    extra_clause: str,
    page_size: int,
) -> bool | None:
    """Walk one side of the scope in uuid order until an eligible row appears.

    Returns True on the first recall-eligible row, False when the side is
    exhausted, and None when the walk hit its row cap without a verdict.
    """
    cursor = ""
    seen = 0
    while seen < _COVERAGE_ROW_CAP:
        # Never read past the cap: the last page is clamped to what remains.
        page = min(page_size, _COVERAGE_ROW_CAP - seen)
        rows = await with_timeout(
            content_client.select_many_raw(
                client,
                f"SELECT {_RAW_MEMORY_RECALL_FIELDS} FROM raw_captures "
                f"WHERE {where_clause}{extra_clause} AND uuid > $coverage_cursor "
                "ORDER BY uuid ASC LIMIT $coverage_limit;",
                **params,
                coverage_cursor=cursor,
                coverage_limit=page,
            ),
            timeout_seconds=content_client.DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS,
            operation_name="surreal_raw_memory_embedding_coverage",
        )
        if not rows:
            return False
        if models.recallable_memories(
            [models.raw_memory_from_record(row) for row in rows], limit=1, as_of=as_of
        ):
            return True
        if len(rows) < page:
            return False
        seen += len(rows)
        cursor = str(rows[-1]["uuid"])
    return None


async def _raw_memory_scope_lacks_embeddings(
    client: SurrealContentClient,
    *,
    where_clause: str,
    params: Mapping[str, object],
    space: models.RawEmbeddingSpace,
    as_of: datetime | None,
    limit: int,
    organization_id: str,
) -> bool | None:
    """Tell whether the scope has recall-eligible captures and none carries a usable vector.

    A usable vector is one from the query's embedding model. A KNN read over
    a scope without one returns nothing and looks identical to a scope with
    no matches, so the raw arm would degrade to BM25 alone without anyone
    noticing. Captures restored from an archive, written while no embedding
    provider was configured, or embedded by the model a deployment switched
    away from land here until the embedding repair runs.

    Eligibility is judged exactly as recall judges it, through the lifecycle
    and as-of filters, so an archived or not-yet-valid row can neither mask a
    missing vector nor raise the report on its own. Each side of the scope is
    walked in uuid order to a fixed row cap. True means vectors are missing,
    False means coverage is fine or the scope is empty, and None means the
    walk hit its cap without a verdict, which the lane reports as unknown
    rather than asserting either way.
    """
    page_size = max(limit * content_client.LIFECYCLE_FILTER_OVERFETCH_FACTOR, 128)
    side_params = {**params, **_raw_vector_space_params(space)}

    async def present(extra_clause: str) -> bool | None:
        return await _eligible_rows_present(
            client,
            where_clause=where_clause,
            params=side_params,
            as_of=as_of,
            extra_clause=extra_clause,
            page_size=page_size,
        )

    try:
        embedded = await present(f" AND embedding != NONE AND {_raw_vector_space_match(space)}")
        if embedded is not False:
            return None if embedded is None else False
        return await present(f" AND {_raw_vector_space_mismatch(space)}")
    except Exception as exc:
        # The probe is diagnostic only; a failed probe must not turn an empty
        # vector read into a failed recall.
        log.warning(
            "raw_memory_embedding_coverage_probe_failed",
            organization_id=organization_id,
            error_type=type(exc).__name__,
        )
        return False


async def _raw_vector_lane_skip(
    client: SurrealContentClient, organization_id: str, space: models.RawEmbeddingSpace
) -> str | None:
    """Why the raw vector lane should stand aside for this model, or None to run it.

    Right after a provider switch almost no capture holds a vector in the new
    model, and an in-bracket model filter then walks the whole index to find
    nothing: about 1.5 s at 20,000 captures on a native server. The raw repair
    records how far it has come in the organization's raw plane state, and the
    lane reads it through the same readiness rule the graph and chunk lanes
    use. Missing or unreadable state runs the lane.
    """
    from sibyl_core.services.content_raw_embedding_repair import RAW_CAPTURE_EMBEDDING_PLANE
    from sibyl_core.services.embedding_lane_readiness import vector_lane_readiness

    async def execute(query: str, **params: object) -> object:
        return await content_client.select_many(client, query, **params)

    readiness = await vector_lane_readiness(
        plane=RAW_CAPTURE_EMBEDDING_PLANE,
        organization_id=organization_id,
        execute=execute,
        query_stamp={
            "provider": space.provider,
            "model": space.model,
            "dimensions": space.dimensions,
        },
    )
    return None if readiness.run else f"vector_lane_{readiness.reason}"


async def raw_memory_query_embedding(query: str) -> RawQueryEmbedding | None:
    provider: EmbeddingProvider | None = None
    try:
        provider = models.configured_raw_memory_embedding_provider()
        if provider is None:
            return None
        space = models.raw_memory_embedding_space(provider.metadata)
        if space is None:
            return None
        embeddings = await provider.embed_texts([query], input_kind="query")
        return RawQueryEmbedding(
            vector=models.embedding_vector_from_batch(embeddings, provider.metadata.dimensions),
            space=space,
        )
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
    capture_ids: Sequence[str] | None = None,
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
        capture_ids=(
            tuple(dict.fromkeys(_normalized_filter_values(capture_ids)))
            if capture_ids is not None
            else None
        ),
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
    source_authority: SourceReadAuthority | None = None,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    source_ids: Sequence[str] | None = None,
    capture_ids: Sequence[str] | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    as_of: datetime | str | None = None,
    limit: int = 10,
    raise_on_source_failure: bool,
) -> RawMemoryRecallResult:
    if source_authority is not None and source_authority.principal_id != principal_id:
        raise ValueError("source authority principal does not match recall principal")
    normalized_query = query.strip()
    if not normalized_query or limit <= 0:
        return RawMemoryRecallResult(())

    normalized_scope = models.coerce_memory_scope(memory_scope)
    filters = _raw_recall_filters(
        source_ids=source_ids,
        capture_ids=capture_ids,
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
    if filters.capture_ids == ():
        return RawMemoryRecallResult(())

    source_results: list[CandidateSourceResult[RawMemory]] = []
    query_embedding: RawQueryEmbedding | None = None
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
        skipped = (
            await _raw_vector_lane_skip(client, organization_id, query_embedding.space)
            if query_embedding is not None
            else None
        )
        if skipped is not None:
            # Almost no capture holds a vector in the query's model yet; the
            # fulltext lane carries the query until the repair converts enough.
            source_results.append(CandidateSourceResult.failed("raw_vector", skipped))
        elif query_embedding is not None:
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
                coverage = (
                    await _raw_memory_scope_lacks_embeddings(
                        client,
                        where_clause=where_clause,
                        params=params,
                        space=query_embedding.space,
                        as_of=effective_as_of,
                        limit=limit,
                        organization_id=organization_id,
                    )
                    if not vector_memories
                    else False
                )
                if coverage is True:
                    log.warning(
                        "raw_memory_vector_recall_unembedded_scope",
                        organization_id=organization_id,
                        memory_scope=normalized_scope.value,
                        has_scope_key=scope_key is not None,
                        provider=query_embedding.space.provider,
                        model=query_embedding.space.model,
                    )
                    source_results.append(
                        CandidateSourceResult.failed("raw_vector", RAW_VECTOR_EMBEDDINGS_MISSING)
                    )
                elif coverage is None:
                    source_results.append(
                        CandidateSourceResult.noted(
                            "raw_vector", vector_memories, RAW_VECTOR_COVERAGE_UNKNOWN
                        )
                    )
                else:
                    source_results.append(
                        CandidateSourceResult.success("raw_vector", vector_memories)
                    )
        from sibyl_core.services.memory_source_validation import SourceReadAuthority

        # The scope the caller already authorized is the ceiling for source
        # validation. Every shared scope key has to reach the authority, or
        # validation reads a listed memory as unavailable and recall drops
        # what listing returns.
        authority = source_authority or SourceReadAuthority(
            principal_id=principal_id,
            projects=frozenset([scope_key])
            if normalized_scope is MemoryScope.PROJECT and scope_key
            else frozenset(),
            teams=frozenset([scope_key])
            if normalized_scope is MemoryScope.TEAM and scope_key
            else frozenset(),
            delegations=frozenset([scope_key])
            if normalized_scope is MemoryScope.DELEGATED and scope_key
            else frozenset(),
        )
        unavailable = await unavailable_publication_ids(
            organization_id,
            {memory.id: memory.metadata for memory in [*fulltext_memories, *vector_memories]},
            raw_memories=[*fulltext_memories, *vector_memories],
            source_authority=authority,
        )

        def current_publication(memory: RawMemory) -> bool:
            return memory.id not in unavailable

        fulltext_memories = list(filter(current_publication, fulltext_memories))
        vector_memories = list(filter(current_publication, vector_memories))
        source_results = [
            replace(source, candidates=tuple(filter(current_publication, source.candidates)))
            for source in source_results
        ]
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
            unavailable = await unavailable_publication_ids(
                organization_id,
                {memory.id: memory.metadata for memory in lexical_memories},
                raw_memories=lexical_memories,
                source_authority=authority,
            )
            lexical_memories = list(filter(current_publication, lexical_memories))
            source_results.append(CandidateSourceResult.success("raw_lexical", lexical_memories))
        return RawMemoryRecallResult(tuple(lexical_memories), tuple(source_results))


async def recall_raw_memory_with_sources(
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    source_authority: SourceReadAuthority | None = None,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    source_ids: Sequence[str] | None = None,
    capture_ids: Sequence[str] | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    as_of: datetime | str | None = None,
    limit: int = 10,
) -> RawMemoryRecallResult:
    """Recall with source receipts; capture IDs restrict UUIDs before source limits.

    An explicit empty capture collection returns no memories. Capture IDs are
    distinct from ingestion source IDs and do not grant access to a capture.
    """
    return await _recall_raw_memory_result(
        organization_id=organization_id,
        principal_id=principal_id,
        query=query,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        source_ids=source_ids,
        capture_ids=capture_ids,
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        as_of=as_of,
        limit=limit,
        raise_on_source_failure=False,
        source_authority=source_authority,
    )


async def recall_raw_memory(
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    source_authority: SourceReadAuthority | None = None,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    source_ids: Sequence[str] | None = None,
    capture_ids: Sequence[str] | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    as_of: datetime | str | None = None,
    limit: int = 10,
) -> list[RawMemory]:
    """Recall authorized captures, optionally restricted to exact capture UUIDs.

    ``capture_ids=None`` leaves membership unrestricted; an empty collection
    returns no memories. Existing scope and publication checks still apply.
    """
    result = await _recall_raw_memory_result(
        organization_id=organization_id,
        principal_id=principal_id,
        query=query,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        source_ids=source_ids,
        capture_ids=capture_ids,
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        as_of=as_of,
        limit=limit,
        raise_on_source_failure=True,
        source_authority=source_authority,
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
    after: tuple[datetime, str] | None = None,
) -> list[RawMemory]:
    if limit <= 0:
        return []
    target_review_state = review_state.strip().lower()
    # A draft whose corrected child was promoted is terminal even though its own
    # row still reads pending: the row is immutable evidence for that child's
    # lineage proof, so its retirement is recorded beside it. Excluding those ids
    # inside the query keeps the page exactly as long as the caller asked for.
    from sibyl_core.services.reflection_supersession import superseded_draft_ids

    retired = (
        await superseded_draft_ids(organization_id) if target_review_state == "pending" else []
    )
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id "
            "AND capture_surface = $capture_surface "
            "AND review_state = $review_state "
            "AND uuid NOT IN $retired "
            "AND ($after_time = NONE OR captured_at > $after_time "
            "OR (captured_at = $after_time AND uuid > $after_id)) "
            "ORDER BY captured_at ASC, uuid ASC LIMIT $limit;",
            organization_id=organization_id,
            capture_surface="reflection_candidate",
            review_state=target_review_state,
            retired=retired,
            limit=limit,
            after_time=after[0] if after else None,
            after_id=after[1] if after else "",
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
            memory.captured_at or memory.created_at or datetime.min.replace(tzinfo=UTC),
            memory.id,
        ),
    )
    return memories[:limit]


async def list_reflection_dream_source_memories(
    *,
    organization_id: str,
    limit: int = 50,
    is_pending: Callable[[RawMemory], Awaitable[bool]] | None = None,
    after_source_id: str = "",
    prefetch: Callable[[list[RawMemory]], Awaitable[None]] | None = None,
) -> list[RawMemory]:
    """Page past excluded rows before applying the eligible-source budget.

    The legacy processed timestamp is diagnostic, not a source-version fence.
    The dream owner supplies its current observation/authority checkpoint test,
    and ``prefetch`` sees each page of eligible rows before their checks run,
    so the owner can batch what the per-row test needs.
    UUID keyset pagination keeps concurrent inserts from shifting page offsets.
    """
    if limit <= 0:
        return []
    result: list[RawMemory] = []
    cursor = after_source_id
    wrapped = not bool(after_source_id)
    page_size = max(50, limit)
    async with content_client.surreal_content_client() as client:
        while len(result) < limit:
            rows = await content_client.select_many(
                client,
                "SELECT * FROM raw_captures "
                "WHERE organization_id = $organization_id AND uuid > $cursor "
                "AND ($upper = NONE OR uuid <= $upper) "
                "AND (capture_surface NOT IN $excluded OR capture_surface = NONE) "
                "ORDER BY uuid ASC LIMIT $limit;",
                organization_id=organization_id,
                cursor=cursor,
                upper=after_source_id if wrapped and after_source_id else None,
                excluded=list(_REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES),
                limit=page_size,
            )
            if not rows:
                if not wrapped:
                    cursor, wrapped = "", True
                    continue
                break
            cursor = str(rows[-1]["uuid"])
            eligible = [
                memory
                for memory in (models.raw_memory_from_record(row) for row in rows)
                if _dream_source_eligible(memory)
            ]
            if prefetch is not None:
                await prefetch(eligible)
            for memory in eligible:
                if is_pending is None or await is_pending(memory):
                    result.append(memory)
                    if len(result) == limit:
                        break
            if len(rows) < page_size:
                if not wrapped:
                    cursor, wrapped = "", True
                else:
                    break
    return result


def _dream_source_eligible(memory: RawMemory) -> bool:
    return (
        models.raw_memory_currently_recallable(memory)
        and models.raw_memory_capture_surface(memory)
        not in _REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES
    )


def _unit_vector(row: Mapping[str, object]) -> tuple[float, ...] | None:
    vector = row.get("embedding")
    if not isinstance(vector, list) or not vector:
        return None
    norm = math.sqrt(math.fsum(float(value) * float(value) for value in vector))
    return tuple(float(value) / norm for value in vector) if norm else None


def _embedding_space(row: Mapping[str, object]) -> str:
    metadata = row.get("metadata")
    space = metadata.get("embedding_metadata") if isinstance(metadata, Mapping) else None
    return json.dumps(space, sort_keys=True, separators=(",", ":"), default=str)


async def list_reflection_dream_neighbours(
    *,
    organization_id: str,
    seed: RawMemory,
    limit: int,
    is_pending: Callable[[RawMemory], Awaitable[bool]] | None = None,
    prefetch: Callable[[list[RawMemory]], Awaitable[None]] | None = None,
) -> list[RawMemory]:
    """The seed's nearest eligible sources that could share its cohort, nearest first.

    Only sources with the seed's owner, scope, scope key, project and
    embedding space can join a cohort with it, so only those are ranked. The
    HNSW index proposes a pool DREAM_NEIGHBOUR_POOL_FACTOR times deeper than
    the request, and every candidate is then ordered by exact cosine similarity
    to the seed from its stored vector, ties broken on identifier. The order
    therefore does not depend on how the index walked its graph; only a source
    the approximate read misses entirely can be absent, and the deep pool keeps
    the nearest sources well inside it. A seed without a vector has no
    neighbours.
    """
    if limit <= 0 or not seed.principal_id:
        return []
    pool = min(max(limit * DREAM_NEIGHBOUR_POOL_FACTOR, limit), KNN_TYPE_OVERFETCH_CAP)
    knn_effort = knn_search_effort(pool, content_client.CONTENT_KNN_EF_FLOOR)
    async with content_client.surreal_content_client() as client:
        anchors = await content_client.select_many(
            client,
            "SELECT embedding, metadata FROM raw_captures "
            "WHERE organization_id = $organization_id AND uuid = $seed LIMIT 1;",
            organization_id=organization_id,
            seed=seed.id,
        )
        anchor = _unit_vector(anchors[0]) if anchors else None
        if anchor is None:
            return []
        rows = await content_client.select_many(
            client,
            "SELECT * FROM raw_captures WITH INDEX idx_raw_captures_embedding "
            "WHERE organization_id = $organization_id AND principal_id = $principal_id "
            f"AND uuid != $seed AND embedding <|{pool}, {knn_effort}|> $vector;",
            organization_id=organization_id,
            principal_id=seed.principal_id,
            seed=seed.id,
            vector=list(anchor),
        )
    space = _embedding_space(anchors[0])
    group = (seed.memory_scope, seed.scope_key, seed.project_id)
    ranked: list[tuple[float, str, RawMemory]] = []
    for row in rows:
        vector = _unit_vector(row)
        if vector is None or len(vector) != len(anchor) or _embedding_space(row) != space:
            continue
        memory = models.raw_memory_from_record(row)
        if (
            memory.principal_id != seed.principal_id
            or (memory.memory_scope, memory.scope_key, memory.project_id) != group
            or not _dream_source_eligible(memory)
        ):
            continue
        similarity = math.fsum(a * b for a, b in zip(anchor, vector, strict=True))
        ranked.append((-similarity, memory.id, memory))
    ranked.sort(key=lambda item: (item[0], item[1]))
    if prefetch is not None:
        await prefetch([memory for _, _, memory in ranked])
    result: list[RawMemory] = []
    for _, _, memory in ranked:
        if is_pending is None or await is_pending(memory):
            result.append(memory)
            if len(result) == limit:
                break
    return result
