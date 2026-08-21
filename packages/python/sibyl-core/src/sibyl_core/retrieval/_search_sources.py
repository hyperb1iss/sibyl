"""Native lexical and vector retrieval lanes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import structlog

from sibyl_core.backends.surreal.fulltext import (
    build_fulltext_terms,
    build_match_disjunction,
)
from sibyl_core.backends.surreal.knn import knn_overfetch_pool, knn_search_effort
from sibyl_core.config import core_config
from sibyl_core.embeddings.providers import EmbeddingMetadata, EmbeddingProvider
from sibyl_core.memory_pipeline.retrieval import CandidateSourceFailure, CandidateSourceResult
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval._search_candidates import (
    _candidate_from_edge_record,
    _candidate_from_episode_record,
    _candidate_from_node_record,
    _candidate_from_raw_memory,
    _record_score,
)
from sibyl_core.retrieval._search_database import _execute_query_records
from sibyl_core.retrieval._search_plan import (
    RetrievalPlan,
    RetrievalSignal,
    SearchFilter,
)
from sibyl_core.retrieval.candidates import RetrievalCandidate, VectorCandidateFetch
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    RawMemoryRecallResult,
    raw_memory_recallable,
)

type RawMemoryRecallFn = Callable[..., Awaitable[list[RawMemory] | RawMemoryRecallResult]]

EDGE_FULLTEXT_MATCH_HEADROOM = 8
EDGE_FULLTEXT_MIN_MATCH_LIMIT = 32
_RAW_MEMORY_CONTEXT_TYPES = {"raw_memory", "session", "episode", "note"}
log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class RawCandidateFetch:
    candidates: list[RetrievalCandidate]
    failures: tuple[CandidateSourceFailure, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


def _candidate_list_or_empty(result: object) -> list[RetrievalCandidate]:
    if isinstance(result, BaseException) or not isinstance(result, list):
        return []
    return cast("list[RetrievalCandidate]", result)


def _candidate_source_result(
    source: str,
    result: object,
) -> CandidateSourceResult[RetrievalCandidate]:
    if isinstance(result, BaseException):
        log.warning(
            "retrieval_candidate_source_failed",
            source=source,
            error_type=type(result).__name__,
        )
        return CandidateSourceResult.failed(source, type(result).__name__)
    if not isinstance(result, list):
        log.warning(
            "retrieval_candidate_source_invalid",
            source=source,
            result_type=type(result).__name__,
        )
        return CandidateSourceResult.failed(source, f"invalid:{type(result).__name__}")
    return CandidateSourceResult.success(source, cast("list[RetrievalCandidate]", result))


def _candidate_source_metadata(
    sources: Sequence[CandidateSourceResult[RetrievalCandidate]],
    *,
    extra_failures: Sequence[CandidateSourceFailure] = (),
) -> dict[str, object]:
    failures = [source.failure for source in sources if source.failure is not None]
    failures.extend(extra_failures)
    metadata: dict[str, object] = {
        "candidate_source_degraded": bool(failures),
        "candidate_source_failure_count": len(failures),
    }
    if failures:
        metadata["candidate_source_failures"] = [failure.as_metadata() for failure in failures]
    return metadata


async def _empty_candidate_source() -> list[RetrievalCandidate]:
    return []


async def _recall_raw_candidates(
    *,
    plan: RetrievalPlan,
    facet: ContextFacet | None,
    requested_types: set[str],
    limit: int,
    recall_fn: RawMemoryRecallFn,
) -> RawCandidateFetch:
    if facet is not None and facet is not ContextFacet.RECENT_MEMORY:
        return RawCandidateFetch([])
    if requested_types and requested_types.isdisjoint(_RAW_MEMORY_CONTEXT_TYPES):
        return RawCandidateFetch([])

    candidates: list[RetrievalCandidate] = []
    failures: list[CandidateSourceFailure] = []
    seen_ids: set[str] = set()
    raw_recall_scopes = {MemoryScope.PRIVATE, MemoryScope.PROJECT, MemoryScope.DELEGATED}
    raw_scopes = [scope for scope in plan.scopes if scope.memory_scope in raw_recall_scopes]
    recalled_by_scope = await asyncio.gather(
        *(
            recall_fn(
                organization_id=plan.organization_id,
                principal_id=scope.principal_id,
                query=plan.query,
                memory_scope=scope.memory_scope.value,
                scope_key=scope.scope_key,
                agent_id=scope.agent_id,
                project_id=scope.project_id,
                limit=limit,
            )
            for scope in raw_scopes
        ),
        return_exceptions=True,
    )
    for scope, recalled in zip(raw_scopes, recalled_by_scope, strict=True):
        if isinstance(recalled, asyncio.CancelledError):
            raise recalled
        if isinstance(recalled, BaseException):
            log.warning(
                "raw_recall_scope_failed",
                error_type=type(recalled).__name__,
                memory_scope=scope.memory_scope.value,
                project_id=scope.project_id,
                scope_key=scope.scope_key,
            )
            failures.append(CandidateSourceFailure("raw_scope_recall", type(recalled).__name__))
            continue
        if isinstance(recalled, RawMemoryRecallResult):
            memories = list(recalled.memories)
            failures.extend(recalled.failures)
        else:
            memories = recalled
        for memory in memories:
            if not raw_memory_recallable(memory):
                continue
            if memory.id in seen_ids:
                continue
            seen_ids.add(memory.id)
            candidates.append(_candidate_from_raw_memory(memory, scope))
    raw_metadata: dict[str, object] = {
        "raw_recall_degraded": bool(failures),
        "raw_recall_failure_count": len(failures),
    }
    if failures:
        raw_metadata["raw_recall_failures"] = [failure.as_metadata() for failure in failures]
    return RawCandidateFetch(
        sorted(candidates, key=lambda candidate: candidate.score, reverse=True),
        failures=tuple(failures),
        metadata=raw_metadata,
    )


async def _node_fulltext_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    limit: int,
) -> list[RetrievalCandidate]:
    match = build_match_disjunction(
        ["name", "summary", "description", "content"],
        build_fulltext_terms(plan.query),
    )
    if match is None:
        return []
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    rows = await _execute_query_records(
        client,
        f"""
        SELECT *,
               {match.score_expr} AS score
        FROM entity
        WHERE """
        + _where_clause(["group_id = $group_id", *filter_clauses])
        + f"""
          AND {match.where_clause}
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        limit=max(int(limit), 1),
        **match.params,
        **filter_params,
    )
    return [
        _candidate_from_node_record(
            row,
            signal=RetrievalSignal.NODE_FULLTEXT,
            score=_record_score(row),
        )
        for row in rows
    ]


async def _exact_key_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    limit: int,
    probe_tokens: Sequence[str],
) -> list[RetrievalCandidate]:
    """Rows whose writer declared one of the query's identifier-shaped tokens.

    This is the one lane that can find a memory whose text never contains the
    query: the key is an assertion layered onto the content, not extracted from
    it. Everything is exact, so the lane cannot return a weakly relevant row and
    the precision problem that keeps BM25 out of fusion does not arise here.

    Inert by construction when the query carries no identifier: no probe tokens
    means no read at all, and the fused pool is exactly what it was before.
    """

    if not probe_tokens:
        return []
    row_limit = max(int(limit), 1)
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    # CONTAINSANY against the index defined on `retrieval_keys_normalized.*`.
    # The element index is what makes this both correct and index-served: on
    # SurrealDB 3.2.3 an index on the bare array field turns this same read into
    # a full table scan, and turns a bare equality into zero rows unless the
    # WHERE clause happens to carry a second predicate. Verified live on 3.2.3
    # (UnionIndexScan across one branch per probe), so the `.*` in the index
    # definition is what this read depends on, not a stylistic choice.
    rows = await _execute_query_records(
        client,
        """
        SELECT *
        FROM entity
        WHERE """
        + _where_clause(["group_id = $group_id", *filter_clauses])
        + """
          AND retrieval_keys_normalized CONTAINSANY $probe_keys
        ORDER BY created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        probe_keys=list(probe_tokens),
        # Read one lane's worth of rows per probe, not one in total. The database
        # truncates by recency, and overlap is only computed here, so a single
        # lane-sized read lets newer single-match rows crowd out the older row
        # that answers every identifier in the query. Probes are capped, so this
        # is the same bounded breadth a read per probe would have had.
        limit=row_limit * len(probe_tokens),
        **filter_params,
    )

    probes = set(probe_tokens)
    scored: list[tuple[int, str, list[str], Mapping[str, object]]] = []
    for row in rows:
        matched = _matched_retrieval_keys(row, probes)
        if not matched:
            continue
        scored.append((len(matched), str(row.get("uuid") or ""), matched, row))
    # A row answering more of the query's identifiers is the better answer, so
    # overlap orders the lane. Counted here rather than in SurrealQL to keep the
    # read to one index lookup and no array functions.
    scored.sort(key=lambda item: (-item[0], item[1]))
    candidates: list[RetrievalCandidate] = []
    # Truncated after the overlap sort, so the lane hands fusion its best rows
    # rather than its newest ones.
    for _count, _uuid, matched, row in scored[:row_limit]:
        candidate = _candidate_from_node_record(
            row,
            signal=RetrievalSignal.EXACT_KEY,
            score=len(matched) / len(probes),
        )
        candidate.metadata["matched_retrieval_keys"] = matched
        candidates.append(candidate)
    return candidates


def _matched_retrieval_keys(
    row: Mapping[str, object],
    probes: set[str],
) -> list[str]:
    stored = row.get("retrieval_keys_normalized")
    if not isinstance(stored, list | tuple):
        return []
    return [str(key) for key in stored if str(key) in probes]


async def _episode_fulltext_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    limit: int,
) -> list[RetrievalCandidate]:
    if search_filter.project_ids:
        return []
    match = build_match_disjunction(["content"], build_fulltext_terms(plan.query))
    if match is None:
        return []
    rows = await _execute_query_records(
        client,
        f"""
        SELECT *, {match.score_expr} AS score
        FROM episode
        WHERE group_id = $group_id
          AND {match.where_clause}
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        limit=max(int(limit), 1),
        **match.params,
    )
    return [
        _candidate_from_episode_record(
            row,
            signal=RetrievalSignal.EPISODE_FULLTEXT,
            score=_record_score(row),
        )
        for row in rows
    ]


async def _edge_fulltext_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    limit: int,
) -> list[RetrievalCandidate]:
    match = build_match_disjunction(["fact"], build_fulltext_terms(plan.query))
    if match is None:
        return []
    result_limit = max(int(limit), 1)
    match_limit = max(result_limit * EDGE_FULLTEXT_MATCH_HEADROOM, EDGE_FULLTEXT_MIN_MATCH_LIMIT)
    match_clauses, match_params = _edge_match_filter_clause(search_filter)
    match_rows = await _execute_query_records(
        client,
        f"""
        SELECT uuid, created_at, {match.score_expr} AS score
        FROM relates_to
        WHERE """
        + _where_clause(["group_id = $group_id", *match_clauses])
        + f"""
          AND {match.where_clause}
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $match_limit;
        """,
        group_id=plan.organization_id,
        match_limit=match_limit,
        **match.params,
        **match_params,
    )
    match_scores: dict[str, float] = {}
    for row in match_rows:
        uuid = str(row.get("uuid") or "")
        if uuid:
            match_scores[uuid] = _record_score(row)
    match_uuids = list(match_scores)
    if search_filter.edge_uuids:
        allowed_edge_uuids = set(search_filter.edge_uuids)
        match_uuids = [uuid for uuid in match_uuids if uuid in allowed_edge_uuids]
    if not match_uuids:
        return []

    hydrate_filter = SearchFilter(
        node_labels=search_filter.node_labels,
        project_ids=search_filter.project_ids,
        edge_types=search_filter.edge_types,
    )
    filter_clauses, filter_params = _edge_filter_clause(hydrate_filter)
    rows = await _execute_query_records(
        client,
        _edge_select()
        + " WHERE "
        + _where_clause(["uuid IN $match_uuids", "group_id = $group_id", *filter_clauses])
        + " LIMIT $limit;",
        match_uuids=match_uuids,
        group_id=plan.organization_id,
        limit=len(match_uuids),
        **filter_params,
    )
    rows_by_uuid = {str(row["uuid"]): row for row in rows if row.get("uuid")}
    candidates = [
        _candidate_from_edge_record(
            rows_by_uuid[uuid],
            signal=RetrievalSignal.EDGE_FULLTEXT,
            score=match_scores[uuid],
        )
        for uuid in match_uuids
        if uuid in rows_by_uuid
    ]
    return candidates[:result_limit]


async def _vector_candidate_sources(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    embedding_provider: EmbeddingProvider | None,
) -> list[list[RetrievalCandidate]]:
    result = await _vector_candidate_sources_detailed(
        client=client,
        plan=plan,
        search_filter=search_filter,
        embedding_provider=embedding_provider,
    )
    return [result.node_candidates, result.edge_candidates]


async def _vector_candidate_sources_detailed(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    embedding_provider: EmbeddingProvider | None,
) -> VectorCandidateFetch:
    vector_requested = (
        RetrievalSignal.NODE_VECTOR in plan.signals or RetrievalSignal.EDGE_VECTOR in plan.signals
    )
    empty = VectorCandidateFetch(
        node_candidates=[],
        edge_candidates=[],
        requested=vector_requested,
        attempted=False,
    )
    if embedding_provider is None:
        return empty
    if not vector_requested:
        return empty
    try:
        embeddings = await embedding_provider.embed_texts([plan.query], input_kind="query")
    except Exception as exc:
        log.warning(
            "vector_embedding_failed",
            organization_id=plan.organization_id,
            query_length=len(plan.query),
            error_type=type(exc).__name__,
        )
        return VectorCandidateFetch(
            node_candidates=[],
            edge_candidates=[],
            requested=True,
            attempted=True,
            failures=(f"embedding:{type(exc).__name__}",),
            reason="embedding_failed",
        )
    try:
        query_embedding = _query_embedding_from_batch(
            embeddings,
            dimensions=embedding_provider.metadata.dimensions,
        )
    except ValueError as exc:
        log.warning(
            "vector_embedding_invalid",
            organization_id=plan.organization_id,
            error=str(exc),
        )
        return VectorCandidateFetch(
            node_candidates=[],
            edge_candidates=[],
            requested=True,
            attempted=True,
            failures=("embedding:invalid",),
            reason="invalid_embedding",
        )
    node_candidates: list[RetrievalCandidate] = []
    edge_candidates: list[RetrievalCandidate] = []
    tasks: list[Awaitable[list[RetrievalCandidate]]] = []
    task_signals: list[RetrievalSignal] = []
    if RetrievalSignal.NODE_VECTOR in plan.signals:
        tasks.append(
            _node_vector_candidates(
                client=client,
                plan=plan,
                search_filter=search_filter,
                query_embedding=query_embedding,
                embedding_metadata=embedding_provider.metadata,
                limit=plan.candidate_limits.node_vector,
            )
        )
        task_signals.append(RetrievalSignal.NODE_VECTOR)
    if RetrievalSignal.EDGE_VECTOR in plan.signals:
        tasks.append(
            _edge_vector_candidates(
                client=client,
                plan=plan,
                search_filter=search_filter,
                query_embedding=query_embedding,
                embedding_metadata=embedding_provider.metadata,
                limit=plan.candidate_limits.edge_vector,
            )
        )
        task_signals.append(RetrievalSignal.EDGE_VECTOR)
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    failures: list[str] = []
    for signal, result in zip(task_signals, gathered, strict=True):
        if isinstance(result, BaseException):
            failures.append(f"{signal.value}:{type(result).__name__}")
            log.warning(
                "vector_query_failed",
                organization_id=plan.organization_id,
                signal=signal.value,
                error_type=type(result).__name__,
            )
            continue
        if signal is RetrievalSignal.NODE_VECTOR:
            node_candidates = _candidate_list_or_empty(result)
        else:
            edge_candidates = _candidate_list_or_empty(result)
    return VectorCandidateFetch(
        node_candidates=node_candidates,
        edge_candidates=edge_candidates,
        requested=True,
        attempted=True,
        failures=tuple(failures),
    )


async def _node_vector_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    query_embedding: Sequence[float],
    embedding_metadata: EmbeddingMetadata,
    limit: int,
) -> list[RetrievalCandidate]:
    if limit <= 0:
        return []
    filter_clauses, filter_params = _node_filter_clause(search_filter)
    candidate_limit = max(int(limit), 1)
    knn_effort = knn_search_effort(candidate_limit, core_config.graph_knn_ef)
    overfetch = search_filter.knn_type_overfetch
    if search_filter.node_types and overfetch > 0:
        # A selective predicate beside the HNSW bracket forces the walk 10-15x
        # deeper regardless of syntax, so the arm walks an untyped pool and
        # filters types outside the bracket. A full head is exactly the typed
        # KNN head; a shortfall falls back to the classic form below.
        pool = knn_overfetch_pool(candidate_limit, overfetch)
        pool_knn_effort = knn_search_effort(pool, core_config.graph_knn_ef)
        overfetch_clauses = [
            clause for clause in filter_clauses if clause != "entity_type IN $node_types"
        ]
        rows = await _execute_query_records(
            client,
            """
            SELECT *
            FROM (
                SELECT *,
                       (1 - vector::distance::knn()) AS score
                FROM entity
                WHERE """
            + _where_clause(["group_id = $group_id", *overfetch_clauses])
            + f"""
                  AND name_embedding <|{pool}, {pool_knn_effort}|> $query_embedding
            )
            WHERE score >= $min_score AND entity_type IN $node_types
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $limit;
            """,
            group_id=plan.organization_id,
            query_embedding=list(query_embedding),
            min_score=plan.vector_min_score,
            limit=candidate_limit,
            **filter_params,
        )
        if len(rows) >= candidate_limit:
            return [
                _candidate_from_node_record(
                    row,
                    signal=RetrievalSignal.NODE_VECTOR,
                    score=_record_score(row),
                    embedding_metadata=embedding_metadata,
                )
                for row in rows
            ]
    rows = await _execute_query_records(
        client,
        """
        SELECT *
        FROM (
            SELECT *,
                   (1 - vector::distance::knn()) AS score
            FROM entity
            WHERE """
        + _where_clause(["group_id = $group_id", *filter_clauses])
        + f"""
              AND name_embedding <|{candidate_limit}, {knn_effort}|> $query_embedding
        )
        WHERE score >= $min_score
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        query_embedding=list(query_embedding),
        min_score=plan.vector_min_score,
        limit=candidate_limit,
        **filter_params,
    )
    return [
        _candidate_from_node_record(
            row,
            signal=RetrievalSignal.NODE_VECTOR,
            score=_record_score(row),
            embedding_metadata=embedding_metadata,
        )
        for row in rows
    ]


async def _edge_vector_candidates(
    *,
    client: Any,
    plan: RetrievalPlan,
    search_filter: SearchFilter,
    query_embedding: Sequence[float],
    embedding_metadata: EmbeddingMetadata,
    limit: int,
) -> list[RetrievalCandidate]:
    if limit <= 0:
        return []
    filter_clauses, filter_params = _edge_filter_clause(search_filter)
    candidate_limit = max(int(limit), 1)
    knn_effort = knn_search_effort(candidate_limit, core_config.graph_knn_ef)
    overfetch = search_filter.knn_type_overfetch
    if search_filter.edge_types and overfetch > 0:
        # Same HNSW planner trap as the node lane; the edge-type filter moves
        # outside the bracket and a shortfall falls back to the classic form.
        pool = knn_overfetch_pool(candidate_limit, overfetch)
        pool_knn_effort = knn_search_effort(pool, core_config.graph_knn_ef)
        overfetch_clauses = [clause for clause in filter_clauses if clause != "name IN $edge_types"]
        rows = await _execute_query_records(
            client,
            "SELECT * FROM ("
            + _edge_select(extra="(1 - vector::distance::knn()) AS score")
            + " WHERE "
            + _where_clause(["group_id = $group_id", *overfetch_clauses])
            + f"""
              AND fact_embedding <|{pool}, {pool_knn_effort}|> $query_embedding
            )
            WHERE score >= $min_score AND name IN $edge_types
            ORDER BY score DESC, created_at DESC, uuid DESC
            LIMIT $limit;
            """,
            group_id=plan.organization_id,
            query_embedding=list(query_embedding),
            min_score=plan.vector_min_score,
            limit=candidate_limit,
            **filter_params,
        )
        if len(rows) >= candidate_limit:
            return [
                _candidate_from_edge_record(
                    row,
                    signal=RetrievalSignal.EDGE_VECTOR,
                    score=_record_score(row),
                    embedding_metadata=embedding_metadata,
                )
                for row in rows
            ]
    rows = await _execute_query_records(
        client,
        "SELECT * FROM ("
        + _edge_select(extra="(1 - vector::distance::knn()) AS score")
        + " WHERE "
        + _where_clause(["group_id = $group_id", *filter_clauses])
        + f"""
          AND fact_embedding <|{candidate_limit}, {knn_effort}|> $query_embedding
        )
        WHERE score >= $min_score
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit;
        """,
        group_id=plan.organization_id,
        query_embedding=list(query_embedding),
        min_score=plan.vector_min_score,
        limit=candidate_limit,
        **filter_params,
    )
    return [
        _candidate_from_edge_record(
            row,
            signal=RetrievalSignal.EDGE_VECTOR,
            score=_record_score(row),
            embedding_metadata=embedding_metadata,
        )
        for row in rows
    ]


def _query_embedding_from_batch(
    embeddings: Sequence[Sequence[float]],
    *,
    dimensions: int,
) -> list[float]:
    if not embeddings:
        raise ValueError("embedding provider returned no vectors")
    embedding = [float(value) for value in embeddings[0]]
    if len(embedding) != dimensions:
        raise ValueError(
            f"embedding provider returned {len(embedding)} dimensions, expected {dimensions}"
        )
    return embedding


def _where_clause(clauses: Sequence[str]) -> str:
    active = [clause for clause in clauses if clause]
    return " AND ".join(active) if active else "true"


def _node_filter_clause(search_filter: SearchFilter) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if search_filter.node_types:
        clauses.append("entity_type IN $node_types")
        params["node_types"] = list(search_filter.node_types)
    if search_filter.node_labels:
        clauses.append("labels CONTAINS $node_label")
        params["node_label"] = search_filter.node_labels[0]
    if search_filter.project_ids:
        clauses.append("(project_id IN $project_ids OR attributes.project_id IN $project_ids)")
        params["project_ids"] = list(search_filter.project_ids)
    return clauses, params


def _edge_filter_clause(
    search_filter: SearchFilter,
    *,
    source_node_uuid: str | None = None,
    target_node_uuid: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if search_filter.edge_uuids:
        clauses.append("uuid IN $edge_uuids")
        params["edge_uuids"] = list(search_filter.edge_uuids)
    if search_filter.edge_types:
        clauses.append("name IN $edge_types")
        params["edge_types"] = list(search_filter.edge_types)
    if search_filter.node_labels:
        clauses.append("in.labels CONTAINS $node_label AND out.labels CONTAINS $node_label")
        params["node_label"] = search_filter.node_labels[0]
    if search_filter.project_ids:
        clauses.append(
            "("
            "attributes.project_id IN $project_ids "
            "OR in.project_id IN $project_ids "
            "OR in.attributes.project_id IN $project_ids "
            "OR out.project_id IN $project_ids "
            "OR out.attributes.project_id IN $project_ids"
            ")"
        )
        params["project_ids"] = list(search_filter.project_ids)
    if source_node_uuid is not None:
        clauses.append("source_id = $source_node_uuid")
        params["source_node_uuid"] = source_node_uuid
    if target_node_uuid is not None:
        clauses.append("target_id = $target_node_uuid")
        params["target_node_uuid"] = target_node_uuid
    return clauses, params


def _edge_match_filter_clause(
    search_filter: SearchFilter,
) -> tuple[list[str], dict[str, Any]]:
    return _edge_filter_clause(search_filter)


def _edge_select(extra: str | None = None) -> str:
    extra_select = f", {extra}" if extra else ""
    return f"""
        SELECT uuid, name, fact, fact_embedding, group_id, episodes, attributes,
               created_at, expired_at, valid_at, invalid_at,
               source_id AS source_node_uuid,
               target_id AS target_node_uuid,
               in.project_id AS source_node_project_id,
               out.project_id AS target_node_project_id{extra_select}
        FROM relates_to
    """
