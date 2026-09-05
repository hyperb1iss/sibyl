"""Entity lookup and ranked search over the native graph."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import structlog

from sibyl_core.backends.surreal.fulltext import (
    build_fulltext_query,
    build_fulltext_terms,
    build_match_disjunction,
)
from sibyl_core.backends.surreal.knn import knn_overfetch_pool, knn_search_effort
from sibyl_core.config import settings
from sibyl_core.embeddings.providers import EmbeddingProvider
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.query_anchors import (
    explicit_query_anchor_proximity_score,
    explicit_query_anchor_score,
    extract_explicit_anchor_phrases,
)
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_common import normalize_graph_records as normalize_records
from sibyl_core.services.graph_common import select_one as _select_one
from sibyl_core.services.graph_embeddings import (
    _embed_texts_with_timeout,
    _embedding_vector_from_batch,
)
from sibyl_core.services.graph_records import _ENTITY_SEARCH_FIELDS, _entity_from_row
from sibyl_core.services.graph_search import (
    bounded_similarity_score as _bounded_similarity_score,
)
from sibyl_core.services.graph_search import (
    merge_ranked_entity_results as _merge_ranked_entity_results,
)
from sibyl_core.services.graph_search import normalize_search_text as _normalize_search_text
from sibyl_core.services.graph_search import row_score as _row_score

log = structlog.get_logger()

_FULLTEXT_FIELDS = ("name", "summary", "description", "content")


def _build_explicit_anchor_search_query(query: str) -> str:
    phrases = extract_explicit_anchor_phrases(query)
    if len(phrases) < 2:
        return ""
    return build_fulltext_query(" ".join(token for phrase in phrases for token in phrase))


def _entity_explicit_anchor_score(query: str, entity: Entity) -> float:
    return explicit_query_anchor_score(query, _entity_anchor_text(entity))


def _entity_anchor_text(entity: Entity) -> str:
    return " ".join(
        part
        for part in (
            entity.name,
            entity.description,
            entity.content,
            str(entity.metadata.get("summary") or ""),
        )
        if part
    )


def _rescue_explicit_anchor_candidate(
    query: str,
    results: Sequence[tuple[Entity, float]],
    anchor_results: Sequence[tuple[Entity, float]],
    *,
    limit: int,
) -> list[tuple[Entity, float]]:
    result_limit = max(int(limit), 1)
    selected = list(results[:result_limit])
    selected_ids = {entity.id for entity, _score in selected}
    candidates = [
        (
            entity,
            score,
            rank,
            explicit_query_anchor_proximity_score(query, _entity_anchor_text(entity)),
        )
        for rank, (entity, score) in enumerate(anchor_results)
        if entity.id not in selected_ids and _entity_explicit_anchor_score(query, entity) >= 1.0
    ]
    if not candidates:
        return selected

    entity, score, _rank, _proximity = max(
        candidates,
        key=lambda item: (item[3], item[1], -item[2]),
    )
    rescue = (entity, score)
    if len(selected) < result_limit:
        selected.append(rescue)
    else:
        selected[-1] = rescue
    return selected


class _EntitySearchManager:
    def __init__(
        self,
        client: SurrealGraphClient,
        *,
        group_id: str,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._client = client
        self._group_id = group_id
        self._embedding_provider = embedding_provider

    async def get(self, entity_id: str) -> Entity:
        row = await _select_one(
            self._client,
            """
            SELECT *
            FROM entity
            WHERE group_id = $group_id AND uuid = $uuid
            LIMIT 1;
            """,
            group_id=self._group_id,
            uuid=entity_id,
        )
        if row is None:
            raise KeyError(entity_id)
        return _entity_from_row(row)

    async def get_many(self, entity_ids: Sequence[str]) -> list[Entity]:
        return [_entity_from_row(row) for row in await self._get_many_rows(entity_ids)]

    async def _get_many_rows(self, entity_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Load scoped raw rows once, in unique requested order, preserving stored fields."""
        ordered_ids = list(dict.fromkeys(str(entity_id) for entity_id in entity_ids if entity_id))
        if not ordered_ids:
            return []
        rows = normalize_records(
            await self._client.execute_query(
                # `uuid IN $list` is never index-served, and here the scan
                # decodes every full row (contents plus embeddings), so each
                # uuid gets its own indexed lookup. The closure body may
                # reference nothing but its own argument: any other binding
                # silently evaluates to nothing on at least one engine, so the
                # group guard is applied to the returned rows instead (uuid is
                # unique table-wide). A missing uuid yields a NONE entry,
                # which normalize_records drops.
                """
                RETURN $uuids.map(|$u|
                    (SELECT * FROM entity WHERE uuid = $u LIMIT 1)[0]
                );
                """,
                uuids=ordered_ids,
            )
        )
        rows_by_id = {
            str(row["uuid"]): row for row in rows if row.get("group_id") == self._group_id
        }
        return [rows_by_id[entity_id] for entity_id in ordered_ids if entity_id in rows_by_id]

    async def get_notes_for_task(self, task_id: str, limit: int = 50) -> list[Entity]:
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT *
                FROM entity
                WHERE group_id = $group_id
                  AND entity_type = 'note'
                  AND task_id = $task_id
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                group_id=self._group_id,
                task_id=task_id,
                limit=max(int(limit), 1),
            )
        )
        return [_entity_from_row(row) for row in rows]

    async def search(
        self,
        *,
        query: str,
        entity_types: Sequence[EntityType] | None = None,
        limit: int = 10,
        knn_type_overfetch: int = 0,
    ) -> list[tuple[Entity, float]]:
        search_query = build_fulltext_query(query)
        if not search_query:
            return []
        result_limit = max(int(limit), 1)
        anchor_search_query = _build_explicit_anchor_search_query(query)

        searches = [
            self._fulltext_search(
                query=query,
                search_query=search_query,
                entity_types=entity_types,
                limit=result_limit,
            ),
            self._vector_search(
                query=query,
                entity_types=entity_types,
                limit=result_limit,
                knn_type_overfetch=knn_type_overfetch,
            ),
        ]
        if anchor_search_query and anchor_search_query != search_query:
            searches.append(
                self._explicit_anchor_fulltext_search(
                    query=query,
                    search_query=anchor_search_query,
                    entity_types=entity_types,
                    limit=result_limit,
                )
            )
        search_results = await asyncio.gather(*searches)
        fulltext_results, vector_results = search_results[:2]
        anchor_results = search_results[2] if len(search_results) > 2 else []

        results = _merge_ranked_entity_results(
            [
                (vector_results, 1.2),
                (fulltext_results, 1.0),
            ],
            limit=result_limit,
        )
        results = _rescue_explicit_anchor_candidate(
            query,
            results,
            anchor_results,
            limit=result_limit,
        )
        if not results:
            results = await self._fallback_text_search(
                query=query,
                entity_types=entity_types,
                limit=limit,
            )
        return results

    async def _fulltext_search(
        self,
        *,
        query: str,
        search_query: str,
        entity_types: Sequence[EntityType] | None,
        limit: int,
    ) -> list[tuple[Entity, float]]:
        """BM25 candidates over every fulltext field, one bounded query per field.

        A single four-field disjunction makes the engine score every matching
        row across all four indexes before LIMIT applies. Splitting it into one
        top-k statement per field, run concurrently, returns the same top-k:
        the merged score is the max across fields, so a row outside a field's
        top-k cannot enter the global top-k when that field already holds k
        rows with equal or greater score. The merge re-applies the statement
        order (score, created_at, uuid) descending before truncating.
        """
        terms = build_fulltext_terms(search_query)
        if not terms:
            return []
        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        matches = [
            match
            for match in (build_match_disjunction([field], terms) for field in _FULLTEXT_FIELDS)
            if match is not None
        ]
        field_rows = await asyncio.gather(
            *(
                self._client.execute_query(
                    "SELECT "
                    + _ENTITY_SEARCH_FIELDS
                    + f""",
                           {match.score_expr} AS score
                    FROM entity
                    WHERE group_id = $group_id
                    """
                    + type_clause
                    + f"""
                      AND {match.where_clause}
                    ORDER BY score DESC, created_at DESC, uuid DESC
                    LIMIT $limit;
                    """,
                    group_id=self._group_id,
                    entity_types=type_values,
                    limit=limit,
                    _query_label="entity.search.fulltext",
                    **match.params,
                )
                for match in matches
            )
        )
        best_by_uuid: dict[str, tuple[Entity, float]] = {}
        for rows in field_rows:
            for row in normalize_records(rows):
                entity = _entity_from_row(row)
                score = _row_score(row)
                current = best_by_uuid.get(entity.id)
                if current is None or score > current[1]:
                    best_by_uuid[entity.id] = (entity, score)
        ranked = sorted(
            best_by_uuid.values(),
            key=lambda item: (item[1], item[0].created_at, item[0].id),
            reverse=True,
        )[:limit]
        return [(entity, _bounded_similarity_score(query, entity)) for entity, _score in ranked]

    async def _explicit_anchor_fulltext_search(
        self,
        *,
        query: str,
        search_query: str,
        entity_types: Sequence[EntityType] | None,
        limit: int,
    ) -> list[tuple[Entity, float]]:
        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        rows = normalize_records(
            await self._client.execute_query(
                "SELECT "
                + _ENTITY_SEARCH_FIELDS
                + """
                FROM entity
                WHERE group_id = $group_id
                """
                + type_clause
                + """
                  AND content @AND@ $search_query
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                group_id=self._group_id,
                search_query=search_query,
                entity_types=type_values,
                limit=limit,
                _query_label="entity.search.fulltext_explicit_anchors",
            )
        )
        return [
            (entity, _bounded_similarity_score(query, entity))
            for entity in (_entity_from_row(row) for row in rows)
        ]

    @staticmethod
    def _typed_overfetch_vector_query(*, candidate_limit: int, overfetch: int) -> str:
        # The inner query walks the HNSW index with only the group predicate;
        # the type filter applies to the materialized pool outside the bracket.
        pool = knn_overfetch_pool(candidate_limit, overfetch)
        overfetch_knn_effort = knn_search_effort(pool, settings.graph_knn_ef)
        return (
            "SELECT * FROM ("
            "SELECT " + _ENTITY_SEARCH_FIELDS + ", (1 - vector::distance::knn()) AS score"
            " FROM entity WHERE group_id = $group_id"
            f" AND name_embedding <|{pool}, {overfetch_knn_effort}|> $query_embedding"
            ") WHERE entity_type IN $entity_types"
            " ORDER BY score DESC, created_at DESC, uuid DESC"
            " LIMIT $limit;"
        )

    async def _vector_search(
        self,
        *,
        query: str,
        entity_types: Sequence[EntityType] | None,
        limit: int,
        knn_type_overfetch: int = 0,
    ) -> list[tuple[Entity, float]]:
        if self._embedding_provider is None:
            return []
        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        candidate_limit = min(max(int(limit) * 4, 32), 200)
        knn_effort = knn_search_effort(candidate_limit, settings.graph_knn_ef)
        try:
            embeddings = await _embed_texts_with_timeout(
                self._embedding_provider,
                [query],
                input_kind="query",
                operation="entity_vector_search",
                timeout_seconds=settings.graph_search_embedding_timeout_seconds,
            )
            query_embedding = _embedding_vector_from_batch(
                embeddings,
                self._embedding_provider.metadata.dimensions,
            )
            rows: list[dict[str, Any]] = []
            if type_values and knn_type_overfetch > 0:
                rows = normalize_records(
                    await self._client.execute_query(
                        self._typed_overfetch_vector_query(
                            candidate_limit=candidate_limit,
                            overfetch=knn_type_overfetch,
                        ),
                        group_id=self._group_id,
                        query_embedding=query_embedding,
                        entity_types=type_values,
                        limit=candidate_limit,
                        _query_label="entity.search.vector.overfetch",
                    )
                )
                if len(rows) >= candidate_limit:
                    return [(_entity_from_row(row), _row_score(row)) for row in rows]
                # Yield shortfall: the query embedding sits in a cluster the
                # requested types do not reach at this depth, so the exactness
                # argument no longer covers the tail. Fall through to the
                # classic typed form, which digs as deep as it needs to.
                log.info(
                    "entity_vector_search_overfetch_fallback",
                    typed_yield=len(rows),
                    candidate_limit=candidate_limit,
                )
            rows = normalize_records(
                await self._client.execute_query(
                    "SELECT *"
                    " FROM ("
                    "SELECT "
                    + _ENTITY_SEARCH_FIELDS
                    + """,
                               (1 - vector::distance::knn()) AS score
                        FROM entity
                        WHERE group_id = $group_id
                    """
                    + type_clause
                    + f"""
                          AND name_embedding <|{candidate_limit}, {knn_effort}|> $query_embedding
                    )
                    ORDER BY score DESC, created_at DESC, uuid DESC
                    LIMIT $limit;
                    """,
                    group_id=self._group_id,
                    query_embedding=query_embedding,
                    entity_types=type_values,
                    limit=candidate_limit,
                    _query_label="entity.search.vector",
                )
            )
        except Exception as exc:
            log.warning(
                "entity_vector_search_failed",
                error_type=type(exc).__name__,
            )
            return []

        return [(_entity_from_row(row), _row_score(row)) for row in rows]

    async def _fallback_text_search(
        self,
        *,
        query: str,
        entity_types: Sequence[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        normalized_query = _normalize_search_text(query)
        if not normalized_query:
            return []

        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        candidate_limit = min(max(int(limit) * 8, 50), 500)
        rows = normalize_records(
            await self._client.execute_query(
                "SELECT "
                + _ENTITY_SEARCH_FIELDS
                + """
                FROM entity
                WHERE group_id = $group_id
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $candidate_limit;
                """,
                group_id=self._group_id,
                entity_types=type_values,
                candidate_limit=candidate_limit,
            )
        )

        scored: list[tuple[Entity, float]] = []
        for row in rows:
            entity = _entity_from_row(row)
            score = _bounded_similarity_score(query, entity)
            if score > 0:
                scored.append((entity, score))

        scored.sort(key=lambda item: (item[1], item[0].created_at, item[0].id), reverse=True)
        return scored[: max(int(limit), 1)]

    async def search_exact_name(
        self,
        query: str,
        *,
        entity_types: Sequence[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        type_values = [entity_type.value for entity_type in entity_types or ()]
        type_clause = "AND entity_type IN $entity_types" if type_values else ""
        rows = normalize_records(
            await self._client.execute_query(
                "SELECT "
                + _ENTITY_SEARCH_FIELDS
                + """
                FROM entity
                WHERE group_id = $group_id
                  AND name = $name_query
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                group_id=self._group_id,
                name_query=query,
                entity_types=type_values,
                limit=max(int(limit), 1),
            )
        )
        return [(_entity_from_row(row), 1.0) for row in rows]
