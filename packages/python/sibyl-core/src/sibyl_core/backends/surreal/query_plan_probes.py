"""Dev-only Surreal query-plan probes for hot retrieval paths."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal, cast

Plane = Literal["content", "graph"]

_INDEX_RE = re.compile(r"\bidx_[A-Za-z0-9_]+\b")
_SCAN_OPERATIONS = frozenset(
    {
        "Iterate Table",
        "Iterate Thing",
        "Iterate Range",
    }
)


@dataclass(frozen=True, slots=True)
class QueryPlanProbe:
    name: str
    plane: Plane
    query: str
    params: dict[str, object]
    expected_indexes: tuple[str, ...]
    max_executed_rows: int | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "plane": self.plane,
            "query": self.query,
            "params": _summarize_params(self.params),
            "expected_indexes": list(self.expected_indexes),
        }
        if self.max_executed_rows is not None:
            payload["max_executed_rows"] = self.max_executed_rows
        return payload


@dataclass(frozen=True, slots=True)
class QueryPlanAnalysis:
    used_indexes: tuple[str, ...]
    uses_expected_index: bool
    scan_operations: tuple[str, ...]
    executed_row_counts: tuple[int, ...]
    max_executed_rows: int | None
    scans_more_than_expected: bool | None

    def to_dict(self) -> dict[str, object]:
        return {
            "used_indexes": list(self.used_indexes),
            "uses_expected_index": self.uses_expected_index,
            "scan_operations": list(self.scan_operations),
            "executed_row_counts": list(self.executed_row_counts),
            "max_executed_rows": self.max_executed_rows,
            "scans_more_than_expected": self.scans_more_than_expected,
        }


@dataclass(frozen=True, slots=True)
class QueryPlanProbeResult:
    probe: QueryPlanProbe
    elapsed_ms: float
    analysis: QueryPlanAnalysis | None
    raw_plan: object | None = None
    error: str | None = None
    error_type: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = self.probe.to_dict()
        payload["elapsed_ms"] = round(self.elapsed_ms, 2)
        payload["analysis"] = self.analysis.to_dict() if self.analysis else None
        if self.error:
            payload["error"] = self.error
            payload["error_type"] = self.error_type
        else:
            payload["raw_plan"] = self.raw_plan
        return payload


def build_hot_query_plan_probes(
    *,
    org_id: str,
    graph_query_embedding: Sequence[float],
    content_query_embedding: Sequence[float],
    query_text: str = "sibyl query plan probe",
    source_ids: Sequence[str] = ("probe-source",),
    project_ids: Sequence[str] = ("probe-project",),
    entity_types: Sequence[str] = (),
    node_types: Sequence[str] = ("task",),
    min_score: float = 0.0,
    similarity_threshold: float = 0.0,
    limit: int = 10,
    graph_knn_effort: int = 40,
) -> tuple[QueryPlanProbe, ...]:
    result_limit = max(int(limit), 1)
    graph_entity_candidate_limit = min(max(result_limit * 4, 32), 200)
    graph_context_candidate_limit = result_limit
    content_candidate_limit = _content_candidate_limit(result_limit)
    source_id_values = _nonempty_strings(source_ids, fallback="probe-source")
    project_id_values = _nonempty_strings(project_ids, fallback="probe-project")
    entity_type_values = tuple(str(value) for value in entity_types if str(value))
    node_type_values = tuple(str(value) for value in node_types if str(value))
    return (
        _graph_entity_vector_probe(
            org_id=org_id,
            query_embedding=graph_query_embedding,
            entity_types=entity_type_values,
            candidate_limit=graph_entity_candidate_limit,
            knn_effort=graph_knn_effort,
        ),
        _context_node_vector_probe(
            org_id=org_id,
            query_embedding=graph_query_embedding,
            node_types=node_type_values,
            project_ids=project_id_values,
            min_score=min_score,
            candidate_limit=graph_context_candidate_limit,
            knn_effort=graph_knn_effort,
        ),
        _context_edge_vector_probe(
            org_id=org_id,
            query_embedding=graph_query_embedding,
            project_ids=project_id_values,
            min_score=min_score,
            candidate_limit=graph_context_candidate_limit,
            knn_effort=graph_knn_effort,
        ),
        _content_rag_chunk_vector_probe(
            source_ids=source_id_values,
            query_embedding=content_query_embedding,
            similarity_threshold=similarity_threshold,
            candidate_limit=content_candidate_limit,
        ),
        _content_hybrid_lexical_probe(
            source_ids=source_id_values,
            query_text=query_text.strip() or "sibyl query plan probe",
            candidate_limit=content_candidate_limit,
        ),
    )


async def run_query_plan_probe(client: Any, probe: QueryPlanProbe) -> QueryPlanProbeResult:
    started = perf_counter()
    try:
        raw_plan = await client.execute_query(probe.query, **probe.params)
    except Exception as exc:
        return QueryPlanProbeResult(
            probe=probe,
            elapsed_ms=(perf_counter() - started) * 1000.0,
            analysis=None,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    analysis = analyze_query_plan(
        raw_plan,
        expected_indexes=probe.expected_indexes,
        max_executed_rows=probe.max_executed_rows,
    )
    return QueryPlanProbeResult(
        probe=probe,
        elapsed_ms=(perf_counter() - started) * 1000.0,
        analysis=analysis,
        raw_plan=raw_plan,
    )


async def run_query_plan_probes(
    clients: Mapping[Plane, Any],
    probes: Sequence[QueryPlanProbe],
) -> tuple[QueryPlanProbeResult, ...]:
    results: list[QueryPlanProbeResult] = []
    for probe in probes:
        client = clients.get(probe.plane)
        if client is None:
            results.append(
                QueryPlanProbeResult(
                    probe=probe,
                    elapsed_ms=0.0,
                    analysis=None,
                    error=f"missing {probe.plane} client",
                    error_type="MissingClient",
                )
            )
            continue
        results.append(await run_query_plan_probe(client, probe))
    return tuple(results)


def analyze_query_plan(
    raw_plan: object,
    *,
    expected_indexes: Sequence[str],
    max_executed_rows: int | None = None,
) -> QueryPlanAnalysis:
    used_indexes = tuple(sorted(set(_extract_index_names(raw_plan))))
    expected = set(expected_indexes)
    row_counts = tuple(_extract_executed_row_counts(raw_plan))
    max_rows = max(row_counts) if row_counts else None
    scans_more_than_expected = (
        max_rows is not None and max_executed_rows is not None and max_rows > max_executed_rows
    )
    return QueryPlanAnalysis(
        used_indexes=used_indexes,
        uses_expected_index=bool(expected & set(used_indexes)),
        scan_operations=tuple(_extract_scan_operations(raw_plan)),
        executed_row_counts=row_counts,
        max_executed_rows=max_rows,
        scans_more_than_expected=scans_more_than_expected
        if max_executed_rows is not None
        else None,
    )


def _graph_entity_vector_probe(
    *,
    org_id: str,
    query_embedding: Sequence[float],
    entity_types: Sequence[str],
    candidate_limit: int,
    knn_effort: int,
) -> QueryPlanProbe:
    type_clause = "      AND entity_type IN $entity_types\n" if entity_types else ""
    query = _explain(
        """
        SELECT *
        FROM (
            SELECT *,
                   (1 - vector::distance::knn()) AS score
            FROM entity
            WHERE group_id = $group_id
"""
        + type_clause
        + f"""
              AND name_embedding <|{candidate_limit}, {knn_effort}|> $query_embedding
        )
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit
        """
    )
    return QueryPlanProbe(
        name="graph.entity_vector_search",
        plane="graph",
        query=query,
        params={
            "group_id": org_id,
            "query_embedding": list(query_embedding),
            "entity_types": list(entity_types),
            "limit": candidate_limit,
        },
        expected_indexes=("idx_entity_embedding",),
    )


def _context_node_vector_probe(
    *,
    org_id: str,
    query_embedding: Sequence[float],
    node_types: Sequence[str],
    project_ids: Sequence[str],
    min_score: float,
    candidate_limit: int,
    knn_effort: int,
) -> QueryPlanProbe:
    clauses = ["group_id = $group_id"]
    if node_types:
        clauses.append("entity_type IN $node_types")
    if project_ids:
        clauses.append("(project_id IN $project_ids OR attributes.project_id IN $project_ids)")
    query = _explain(
        f"""
        SELECT *
        FROM (
            SELECT *,
                   (1 - vector::distance::knn()) AS score
            FROM entity
            WHERE {_where_clause(clauses)}
              AND name_embedding <|{candidate_limit}, {knn_effort}|> $query_embedding
        )
        WHERE score >= $min_score
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit
        """
    )
    return QueryPlanProbe(
        name="context.node_vector_search",
        plane="graph",
        query=query,
        params={
            "group_id": org_id,
            "query_embedding": list(query_embedding),
            "node_types": list(node_types),
            "project_ids": list(project_ids),
            "min_score": min_score,
            "limit": candidate_limit,
        },
        expected_indexes=("idx_entity_embedding",),
    )


def _context_edge_vector_probe(
    *,
    org_id: str,
    query_embedding: Sequence[float],
    project_ids: Sequence[str],
    min_score: float,
    candidate_limit: int,
    knn_effort: int,
) -> QueryPlanProbe:
    clauses = ["group_id = $group_id"]
    if project_ids:
        clauses.append(
            "("
            "attributes.project_id IN $project_ids "
            "OR in.project_id IN $project_ids "
            "OR in.attributes.project_id IN $project_ids "
            "OR out.project_id IN $project_ids "
            "OR out.attributes.project_id IN $project_ids"
            ")"
        )
    query = _explain(
        """
        SELECT *
        FROM (
            SELECT uuid, name, fact, fact_embedding, group_id, episodes, attributes,
                   created_at, expired_at, valid_at, invalid_at,
                   in.uuid AS source_node_uuid,
                   out.uuid AS target_node_uuid,
                   in.project_id AS source_node_project_id,
                   out.project_id AS target_node_project_id,
                   (1 - vector::distance::knn()) AS score
            FROM relates_to
            WHERE """
        + _where_clause(clauses)
        + f"""
              AND fact_embedding <|{candidate_limit}, {knn_effort}|> $query_embedding
        )
        WHERE score >= $min_score
        ORDER BY score DESC, created_at DESC, uuid DESC
        LIMIT $limit
        """
    )
    return QueryPlanProbe(
        name="context.edge_vector_search",
        plane="graph",
        query=query,
        params={
            "group_id": org_id,
            "query_embedding": list(query_embedding),
            "project_ids": list(project_ids),
            "min_score": min_score,
            "limit": candidate_limit,
        },
        expected_indexes=("idx_relates_fact_embedding",),
    )


def _content_rag_chunk_vector_probe(
    *,
    source_ids: Sequence[str],
    query_embedding: Sequence[float],
    similarity_threshold: float,
    candidate_limit: int,
) -> QueryPlanProbe:
    query = (
        "LET $document_ids = ("
        "SELECT VALUE uuid FROM crawled_documents WHERE source_id INSIDE $source_ids"
        ");"
        + _explain(
            f"""
            SELECT *
            FROM (
                SELECT uuid, document_id, chunk_index, chunk_type, content, context,
                       heading_path, language, has_entities, entity_ids,
                       (1 - vector::distance::knn()) AS score
                FROM document_chunks
                WHERE document_id INSIDE $document_ids
                  AND embedding <|{candidate_limit}, 40|> $query_embedding
            )
            WHERE score >= $similarity_threshold
            ORDER BY score DESC
            LIMIT $candidate_limit
            """
        )
    )
    return QueryPlanProbe(
        name="content.rag_chunk_vector_search",
        plane="content",
        query=query,
        params={
            "source_ids": list(source_ids),
            "query_embedding": list(query_embedding),
            "similarity_threshold": similarity_threshold,
            "candidate_limit": candidate_limit,
        },
        expected_indexes=("idx_document_chunks_embedding",),
    )


def _content_hybrid_lexical_probe(
    *,
    source_ids: Sequence[str],
    query_text: str,
    candidate_limit: int,
) -> QueryPlanProbe:
    query = (
        "LET $document_ids = ("
        "SELECT VALUE uuid FROM crawled_documents WHERE source_id INSIDE $source_ids"
        ");"
        + _explain(
            """
            SELECT uuid, document_id, chunk_index, chunk_type, content, context,
                   heading_path, language, has_entities, entity_ids,
                   search::score(0) AS score
            FROM document_chunks
            WHERE document_id INSIDE $document_ids
              AND content @0@ $search_query
            ORDER BY score DESC
            LIMIT $candidate_limit
            """
        )
    )
    return QueryPlanProbe(
        name="content.hybrid_lexical_search",
        plane="content",
        query=query,
        params={
            "source_ids": list(source_ids),
            "search_query": query_text,
            "candidate_limit": candidate_limit,
        },
        expected_indexes=("idx_document_chunks_content_ft",),
    )


def _explain(select_query: str) -> str:
    return f"{select_query.strip().removesuffix(';')} EXPLAIN FULL;"


def _where_clause(clauses: Sequence[str]) -> str:
    active = [clause for clause in clauses if clause]
    return " AND ".join(active) if active else "true"


def _content_candidate_limit(limit: int) -> int:
    return min(max(limit * 5, limit, 1), 100)


def _nonempty_strings(values: Sequence[str], *, fallback: str) -> tuple[str, ...]:
    cleaned = tuple(str(value) for value in values if str(value))
    return cleaned or (fallback,)


def _summarize_params(params: Mapping[str, object]) -> dict[str, object]:
    return {key: _summarize_value(value) for key, value in params.items()}


def _summarize_value(value: object) -> object:
    if (
        isinstance(value, list)
        and len(value) > 8
        and all(isinstance(item, int | float) for item in value)
    ):
        return {"type": "vector", "length": len(value)}
    return value


def _extract_index_names(value: object) -> list[str]:
    return sorted(set(_INDEX_RE.findall(_stringify_plan(value))))


def _extract_scan_operations(value: object) -> list[str]:
    operations: set[str] = set()
    for item in _walk_plan(value):
        if not isinstance(item, Mapping):
            continue
        mapping = cast("Mapping[str, object]", item)
        operation = mapping.get("operation")
        if not isinstance(operation, str) or operation not in _SCAN_OPERATIONS:
            continue
        table = (
            mapping.get("table")
            or mapping.get("thing")
            or _nested_value(mapping.get("detail"), "table")
        )
        if table:
            operations.add(f"{operation}: {table}")
        else:
            operations.add(operation)
    return sorted(operations)


def _extract_executed_row_counts(value: object) -> list[int]:
    counts: list[int] = []
    for item in _walk_plan(value):
        if isinstance(item, Mapping):
            mapping = cast("Mapping[str, object]", item)
            count = mapping.get("count")
            if isinstance(count, int):
                counts.append(count)
    return counts


def _nested_value(value: object, key: str) -> object | None:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[str, object]", value)
        direct = mapping.get(key)
        if direct is not None:
            return direct
        for child in mapping.values():
            found = _nested_value(child, key)
            if found is not None:
                return found
    if isinstance(value, list | tuple):
        for child in value:
            found = _nested_value(child, key)
            if found is not None:
                return found
    return None


def _stringify_plan(value: object) -> str:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[str, object]", value)
        return " ".join(f"{key} {_stringify_plan(child)}" for key, child in sorted(mapping.items()))
    if isinstance(value, list | tuple):
        return " ".join(_stringify_plan(child) for child in value)
    return str(value)


def _walk_plan(value: object) -> list[object]:
    items = [value]
    if isinstance(value, Mapping):
        mapping = cast("Mapping[str, object]", value)
        for child in mapping.values():
            items.extend(_walk_plan(child))
    elif isinstance(value, list | tuple):
        for child in value:
            items.extend(_walk_plan(child))
    return items
