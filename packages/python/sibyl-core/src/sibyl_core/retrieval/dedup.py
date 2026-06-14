"""Entity deduplication using embedding similarity.

Detects and merges duplicate entities based on semantic similarity
of their embeddings. Redirects relationships during merge.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar
from uuid import uuid4

import numpy as np
import structlog

from sibyl_core.config import settings
from sibyl_core.models.entities import Entity
from sibyl_core.services.graph import normalize_records

log = structlog.get_logger()

T = TypeVar("T")


@dataclass
class DedupConfig:
    """Configuration for entity deduplication.

    Attributes:
        similarity_threshold: Minimum cosine similarity to consider duplicates (0.0-1.0).
        batch_size: Number of entities to process per batch.
        same_type_only: Only compare entities of the same type.
        min_name_overlap: Minimum Jaccard similarity of names (extra filter).
        scope_metadata_keys: Metadata fields that must match exactly when resolving
            incoming entities against existing graph rows.
    """

    similarity_threshold: float = 0.95
    batch_size: int = 100
    same_type_only: bool = True
    min_name_overlap: float = 0.3
    scope_metadata_keys: tuple[str, ...] = ()


@dataclass
class DuplicatePair:
    """A pair of entities identified as potential duplicates.

    Attributes:
        entity1_id: ID of first entity.
        entity2_id: ID of second entity.
        similarity: Cosine similarity score.
        entity1_name: Name of first entity (for display).
        entity2_name: Name of second entity (for display).
        entity_type: Type of the entities.
        suggested_keep: Which entity ID is suggested to keep.
    """

    entity1_id: str
    entity2_id: str
    similarity: float
    entity1_name: str = ""
    entity2_name: str = ""
    entity_type: str = ""
    suggested_keep: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "entity1_id": self.entity1_id,
            "entity2_id": self.entity2_id,
            "similarity": round(self.similarity, 4),
            "entity1_name": self.entity1_name,
            "entity2_name": self.entity2_name,
            "entity_type": self.entity_type,
            "suggested_keep": self.suggested_keep,
        }


def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Calculate cosine similarity between two vectors.

    Args:
        vec1: First vector.
        vec2: Second vector.

    Returns:
        Cosine similarity (-1.0 to 1.0, typically 0.0 to 1.0 for embeddings).
    """
    if len(vec1) != len(vec2):
        return 0.0

    if not vec1 or not vec2:
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=True))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)


def jaccard_similarity(s1: str, s2: str) -> float:
    """Calculate Jaccard similarity between two strings (word-level).

    Args:
        s1: First string.
        s2: Second string.

    Returns:
        Jaccard similarity (0.0 to 1.0).
    """
    words1 = set(s1.lower().split())
    words2 = set(s2.lower().split())

    if not words1 and not words2:
        return 1.0
    if not words1 or not words2:
        return 0.0

    intersection = len(words1 & words2)
    union = len(words1 | words2)

    return intersection / union if union > 0 else 0.0


def _float_list(value: object) -> list[float]:
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        if isinstance(item, int | float):
            out.append(float(item))
    return out


def _dedup_seed_from_row(row: dict[str, object]) -> tuple[str, str, str, list[float]] | None:
    entity_id = str(row.get("uuid") or "")
    entity_type = str(row.get("entity_type") or "")
    embedding = _float_list(row.get("name_embedding"))
    if not entity_id or not entity_type or not embedding:
        return None
    return entity_id, str(row.get("name") or ""), entity_type, embedding


def _dedup_candidate_from_row(row: dict[str, object]) -> tuple[str, str, str, float] | None:
    entity_id = str(row.get("uuid") or "")
    entity_type = str(row.get("entity_type") or "")
    if not entity_id or not entity_type:
        return None
    score = row.get("score")
    if not isinstance(score, int | float):
        return None
    return entity_id, str(row.get("name") or ""), entity_type, float(score)


def _dedup_candidate_with_seed_from_row(
    row: dict[str, object],
) -> tuple[str, str, str, str, float] | None:
    seed_id = str(row.get("seed_id") or "")
    candidate = _dedup_candidate_from_row(row)
    if not seed_id or candidate is None:
        return None
    entity_id, name, entity_type, score = candidate
    return seed_id, entity_id, name, entity_type, score


def _append_scope_constraint_clauses(
    clauses: list[str],
    params: dict[str, Any],
    scope_constraints: dict[str, object | None] | None,
    *,
    param_prefix: str,
) -> None:
    if not scope_constraints:
        return
    for key, value in scope_constraints.items():
        field = f"attributes.{key}"
        if value is None:
            clauses.append(f"{field} = NONE")
            continue
        param_name = f"{param_prefix}_{key}"
        clauses.append(f"{field} = ${param_name}")
        params[param_name] = value


@dataclass
class EntityDeduplicator:
    """Detects and merges duplicate entities.

    Uses embedding similarity to find potential duplicates,
    with optional name overlap filtering.

    Usage:
        dedup = EntityDeduplicator(client, entity_manager)
        pairs = await dedup.find_duplicates()
        for pair in pairs:
            print(f"Duplicate: {pair.entity1_name} <-> {pair.entity2_name}")
        # Review and merge
        await dedup.merge_entities(keep_id="id1", remove_id="id2")
    """

    client: Any
    entity_manager: Any
    config: DedupConfig = field(default_factory=DedupConfig)

    # Internal state
    _duplicate_pairs: list[DuplicatePair] = field(default_factory=list, init=False)

    def _require_group_id(self) -> str:
        """Require org scope for multi-tenant dedup queries."""
        group_id = getattr(self.entity_manager, "_group_id", None)
        if not group_id:
            raise ValueError("group_id is required for dedup operations")
        return str(group_id)

    async def find_duplicates(
        self,
        entity_types: list[str] | None = None,
        threshold: float | None = None,
    ) -> list[DuplicatePair]:
        """Find potential duplicate entities based on embedding similarity.

        Args:
            entity_types: Filter to specific entity types.
            threshold: Override similarity threshold from config.

        Returns:
            List of duplicate pairs sorted by similarity (highest first).
        """
        similarity_threshold = threshold or self.config.similarity_threshold

        log.info(
            "find_duplicates_start",
            threshold=similarity_threshold,
            entity_types=entity_types,
        )

        hnsw_result = await self._find_similar_pairs_hnsw(entity_types, similarity_threshold)
        if hnsw_result is not None:
            pairs, entity_count = hnsw_result
            if entity_count < 2:
                log.info("find_duplicates_insufficient_entities", count=entity_count)
                return []
        else:
            entities = await self._fetch_entities_with_embeddings(entity_types)
            if len(entities) < 2:
                log.info("find_duplicates_insufficient_entities", count=len(entities))
                return []
            entity_count = len(entities)
            pairs = self._find_similar_pairs_vectorized(entities, similarity_threshold)

        # Sort by similarity (highest first)
        pairs.sort(key=lambda p: p.similarity, reverse=True)

        self._duplicate_pairs = pairs

        log.info(
            "find_duplicates_complete",
            total_entities=entity_count,
            duplicate_pairs=len(pairs),
            candidate_strategy="hnsw" if hnsw_result is not None else "vectorized",
        )

        return pairs

    async def _find_similar_pairs_hnsw(
        self,
        entity_types: list[str] | None,
        threshold: float,
    ) -> tuple[list[DuplicatePair], int] | None:
        execute_query = getattr(self.client, "execute_query", None)
        if not callable(execute_query) or not inspect.iscoroutinefunction(execute_query):
            return None
        execute_query_raw = getattr(self.client, "execute_query_raw", None)
        if not callable(execute_query_raw) or not inspect.iscoroutinefunction(execute_query_raw):
            execute_query_raw = None

        group_id = self._require_group_id()
        allowed_types = [entity_type.lower() for entity_type in entity_types or []]
        type_clause = "AND entity_type IN $entity_types" if allowed_types else ""
        page_size = max(self.config.batch_size, 100)
        pairs: list[DuplicatePair] = []
        seen_pairs: set[tuple[str, str]] = set()
        entity_count = 0
        offset = 0

        try:
            while True:
                seed_rows = normalize_records(
                    await execute_query(
                        """
                        SELECT uuid, name, entity_type, name_embedding
                        FROM entity
                        WHERE group_id = $group_id
                          AND name_embedding != NONE
                    """
                        + type_clause
                        + """
                        ORDER BY updated_at DESC, created_at DESC, uuid DESC
                        START $offset LIMIT $limit;
                        """,
                        group_id=group_id,
                        entity_types=allowed_types,
                        offset=offset,
                        limit=page_size,
                        _query_label="dedup.seeds",
                    )
                )
                if not seed_rows:
                    break

                offset += len(seed_rows)
                seeds = [seed for row in seed_rows if (seed := _dedup_seed_from_row(row))]
                entity_count += len(seeds)
                pairs.extend(
                    await self._find_hnsw_candidates_for_seeds(
                        seeds,
                        group_id=group_id,
                        entity_types=allowed_types,
                        threshold=threshold,
                        seen_pairs=seen_pairs,
                        execute_query=execute_query,
                        execute_query_raw=execute_query_raw,
                    )
                )

                if len(seed_rows) < page_size:
                    break
        except Exception as exc:
            log.warning(
                "find_duplicates_hnsw_failed",
                error_type=type(exc).__name__,
            )
            return None

        return pairs, entity_count

    async def resolve_existing_entities(
        self,
        entities: Sequence[Entity],
        *,
        threshold: float | None = None,
    ) -> dict[str, DuplicatePair]:
        execute_query = getattr(self.client, "execute_query", None)
        if not callable(execute_query) or not inspect.iscoroutinefunction(execute_query):
            return {}
        execute_query_raw = getattr(self.client, "execute_query_raw", None)
        if not callable(execute_query_raw) or not inspect.iscoroutinefunction(execute_query_raw):
            execute_query_raw = None

        seeds: list[tuple[str, str, str, list[float]]] = []
        scope_constraints: dict[str, dict[str, object | None]] = {}
        for entity in entities:
            embedding = _float_list(entity.embedding)
            if entity.id and embedding:
                seeds.append((entity.id, entity.name, entity.entity_type.value, embedding))
                if self.config.scope_metadata_keys:
                    metadata = entity.metadata if isinstance(entity.metadata, dict) else {}
                    scope_constraints[entity.id] = {
                        key: metadata.get(key) for key in self.config.scope_metadata_keys
                    }
        if not seeds:
            return {}

        try:
            pairs = await self._find_hnsw_candidates_for_seeds(
                seeds,
                group_id=self._require_group_id(),
                entity_types=[],
                threshold=threshold or self.config.similarity_threshold,
                seen_pairs=set(),
                execute_query=execute_query,
                execute_query_raw=execute_query_raw,
                scope_constraints=scope_constraints,
            )
        except Exception as exc:
            log.warning(
                "resolve_existing_entities_hnsw_failed",
                error_type=type(exc).__name__,
            )
            return {}

        best_by_entity_id: dict[str, DuplicatePair] = {}
        for pair in sorted(pairs, key=lambda item: item.similarity, reverse=True):
            best_by_entity_id.setdefault(pair.entity1_id, pair)
        return best_by_entity_id

    async def _find_hnsw_candidates_for_seeds(
        self,
        seeds: Sequence[tuple[str, str, str, list[float]]],
        *,
        group_id: str,
        entity_types: list[str],
        threshold: float,
        seen_pairs: set[tuple[str, str]],
        execute_query: Any,
        execute_query_raw: Any | None = None,
        scope_constraints: dict[str, dict[str, object | None]] | None = None,
    ) -> list[DuplicatePair]:
        if not seeds:
            return []

        candidate_limit = max(2, min(self.config.batch_size, 100))
        if execute_query_raw is None and len(seeds) > 1:
            pairs: list[DuplicatePair] = []
            for seed in seeds:
                pairs.extend(
                    await self._find_hnsw_candidates_for_seed(
                        seed,
                        group_id=group_id,
                        entity_types=entity_types,
                        threshold=threshold,
                        candidate_limit=candidate_limit,
                        seen_pairs=seen_pairs,
                        execute_query=execute_query,
                        scope_constraints=(scope_constraints or {}).get(seed[0]),
                    )
                )
            return pairs

        knn_effort = max(1, int(settings.graph_knn_ef))
        statements: list[str] = []
        params: dict[str, Any] = {
            "group_id": group_id,
            "threshold": threshold,
            "entity_types": entity_types,
        }
        for index, (seed_id, _seed_name, seed_type, seed_embedding) in enumerate(seeds):
            seed_id_param = f"seed_id_{index}"
            seed_type_param = f"seed_type_{index}"
            seed_embedding_param = f"seed_embedding_{index}"
            limit_param = f"limit_{index}"
            scope = (scope_constraints or {}).get(seed_id)
            clauses = [
                "group_id = $group_id",
                f"uuid != ${seed_id_param}",
                "name_embedding != NONE",
            ]
            if self.config.same_type_only:
                clauses.append(f"entity_type = ${seed_type_param}")
            elif entity_types:
                clauses.append("entity_type IN $entity_types")
            _append_scope_constraint_clauses(
                clauses,
                params,
                scope,
                param_prefix=f"scope_{index}",
            )

            params[seed_id_param] = seed_id
            params[seed_type_param] = seed_type
            params[seed_embedding_param] = seed_embedding
            params[limit_param] = candidate_limit
            statements.append(
                f"""
                SELECT seed_id, uuid, name, entity_type, score, created_at
                FROM (
                    SELECT ${seed_id_param} AS seed_id,
                           uuid, name, entity_type, created_at,
                           (1 - vector::distance::knn()) AS score
                    FROM entity
                    WHERE """
                + " AND ".join(clauses)
                + f"""
                      AND name_embedding <|{candidate_limit}, {knn_effort}|> ${seed_embedding_param}
                )
                WHERE score >= $threshold
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT ${limit_param};
                """
            )

        rows = normalize_records(
            await (execute_query_raw or execute_query)(
                "\n".join(statements),
                **params,
                _query_label="dedup.candidates.batch",
            )
        )
        seeds_by_id = {
            seed_id: (seed_name, seed_type) for seed_id, seed_name, seed_type, _ in seeds
        }

        pairs: list[DuplicatePair] = []
        for row in rows:
            candidate = _dedup_candidate_with_seed_from_row(row)
            if candidate is None:
                continue
            seed_id, candidate_id, candidate_name, candidate_type, similarity = candidate
            seed = seeds_by_id.get(seed_id)
            if seed is None:
                continue
            seed_name, seed_type = seed
            pair = self._candidate_pair(
                seed_id=seed_id,
                seed_name=seed_name,
                seed_type=seed_type,
                candidate_id=candidate_id,
                candidate_name=candidate_name,
                candidate_type=candidate_type,
                similarity=similarity,
                seen_pairs=seen_pairs,
            )
            if pair is not None:
                pairs.append(pair)
        return pairs

    async def _find_hnsw_candidates_for_seed(
        self,
        seed: tuple[str, str, str, list[float]],
        *,
        group_id: str,
        entity_types: list[str],
        threshold: float,
        candidate_limit: int,
        seen_pairs: set[tuple[str, str]],
        execute_query: Any,
        scope_constraints: dict[str, object | None] | None = None,
    ) -> list[DuplicatePair]:
        seed_id, seed_name, seed_type, seed_embedding = seed
        clauses = [
            "group_id = $group_id",
            "uuid != $seed_id",
            "name_embedding != NONE",
        ]
        params: dict[str, Any] = {
            "group_id": group_id,
            "seed_id": seed_id,
            "seed_embedding": seed_embedding,
            "threshold": threshold,
            "limit": candidate_limit,
            "entity_types": entity_types,
        }
        if self.config.same_type_only:
            clauses.append("entity_type = $seed_type")
            params["seed_type"] = seed_type
        elif entity_types:
            clauses.append("entity_type IN $entity_types")
        _append_scope_constraint_clauses(
            clauses,
            params,
            scope_constraints,
            param_prefix="scope",
        )

        knn_effort = max(1, int(settings.graph_knn_ef))
        rows = normalize_records(
            await execute_query(
                """
                SELECT uuid, name, entity_type, score, created_at
                FROM (
                    SELECT uuid, name, entity_type, created_at,
                           (1 - vector::distance::knn()) AS score
                    FROM entity
                    WHERE """
                + " AND ".join(clauses)
                + f"""
                      AND name_embedding <|{candidate_limit}, {knn_effort}|> $seed_embedding
                )
                WHERE score >= $threshold
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                **params,
                _query_label="dedup.candidates",
            )
        )

        pairs: list[DuplicatePair] = []
        for row in rows:
            candidate = _dedup_candidate_from_row(row)
            if candidate is None:
                continue
            candidate_id, candidate_name, candidate_type, similarity = candidate
            pair = self._candidate_pair(
                seed_id=seed_id,
                seed_name=seed_name,
                seed_type=seed_type,
                candidate_id=candidate_id,
                candidate_name=candidate_name,
                candidate_type=candidate_type,
                similarity=similarity,
                seen_pairs=seen_pairs,
            )
            if pair is not None:
                pairs.append(pair)
        return pairs

    def _candidate_pair(
        self,
        *,
        seed_id: str,
        seed_name: str,
        seed_type: str,
        candidate_id: str,
        candidate_name: str,
        candidate_type: str,
        similarity: float,
        seen_pairs: set[tuple[str, str]],
    ) -> DuplicatePair | None:
        first_id, second_id = sorted((seed_id, candidate_id))
        pair_key = (first_id, second_id)
        if pair_key in seen_pairs:
            return None
        seen_pairs.add(pair_key)

        if self.config.same_type_only and seed_type != candidate_type:
            return None
        if self.config.min_name_overlap > 0:
            name_sim = jaccard_similarity(seed_name, candidate_name)
            if name_sim < self.config.min_name_overlap:
                return None

        return DuplicatePair(
            entity1_id=seed_id,
            entity2_id=candidate_id,
            similarity=similarity,
            entity1_name=seed_name,
            entity2_name=candidate_name,
            entity_type=seed_type,
            suggested_keep=self._suggest_keep(
                seed_id,
                candidate_id,
                seed_name,
                candidate_name,
            ),
        )

    def suggest_merges(self) -> list[DuplicatePair]:
        """Return the current list of suggested merges.

        Call find_duplicates() first to populate this list.

        Returns:
            List of duplicate pairs with merge suggestions.
        """
        return self._duplicate_pairs

    def _find_similar_pairs_vectorized(
        self,
        entities: list[tuple[str, str, str, list[float]]],
        threshold: float,
    ) -> list[DuplicatePair]:
        """Find similar entity pairs using numpy vectorized operations.

        Uses matrix multiplication for cosine similarity computation,
        which is ~100x faster than Python loops due to SIMD optimization.

        Args:
            entities: List of (id, name, type, embedding) tuples.
            threshold: Minimum similarity threshold.

        Returns:
            List of DuplicatePair objects for pairs above threshold.
        """
        n = len(entities)
        if n < 2:
            return []

        # Extract data into numpy arrays
        ids = [e[0] for e in entities]
        names = [e[1] for e in entities]
        types = [e[2] for e in entities]
        embeddings = np.array([e[3] for e in entities], dtype=np.float32)

        # Normalize embeddings for cosine similarity
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        # Avoid division by zero
        norms = np.maximum(norms, 1e-10)
        normalized = embeddings / norms

        # Compute similarity matrix via dot product (cosine similarity of normalized vectors)
        similarity_matrix = normalized @ normalized.T

        # Find pairs above threshold (only upper triangle to avoid duplicates)
        pairs: list[DuplicatePair] = []
        indices = np.triu_indices(n, k=1)  # Upper triangle, k=1 excludes diagonal

        for idx in range(len(indices[0])):
            i, j = indices[0][idx], indices[1][idx]
            sim = float(similarity_matrix[i, j])

            if sim < threshold:
                continue

            # Skip if different types (when same_type_only)
            if self.config.same_type_only and types[i] != types[j]:
                continue

            # Optional: check name overlap as secondary filter
            if self.config.min_name_overlap > 0:
                name_sim = jaccard_similarity(names[i], names[j])
                if name_sim < self.config.min_name_overlap:
                    continue

            # Suggest keeping the entity with more content/metadata
            suggested_keep = self._suggest_keep(ids[i], ids[j], names[i], names[j])

            pairs.append(
                DuplicatePair(
                    entity1_id=ids[i],
                    entity2_id=ids[j],
                    similarity=sim,
                    entity1_name=names[i],
                    entity2_name=names[j],
                    entity_type=types[i],
                    suggested_keep=suggested_keep,
                )
            )

        return pairs

    async def merge_entities(
        self,
        keep_id: str,
        remove_id: str,
        merge_metadata: bool = True,
    ) -> bool:
        """Merge two entities, redirecting relationships.

        Args:
            keep_id: ID of entity to keep.
            remove_id: ID of entity to remove.
            merge_metadata: Whether to merge metadata from removed entity.

        Returns:
            True if merge succeeded, False otherwise.
        """
        log.info(
            "merge_entities_start",
            keep_id=keep_id,
            remove_id=remove_id,
            merge_metadata=merge_metadata,
        )

        try:
            # Fetch both entities
            keep_entity = await self.entity_manager.get(keep_id)
            remove_entity = await self.entity_manager.get(remove_id)

            if not keep_entity or not remove_entity:
                log.warning(
                    "merge_entities_not_found",
                    keep_found=keep_entity is not None,
                    remove_found=remove_entity is not None,
                )
                return False

            if merge_metadata and remove_entity.metadata:
                merged_meta = {**remove_entity.metadata, **(keep_entity.metadata or {})}
                await self.entity_manager.update(keep_id, {"metadata": merged_meta})

            await self._redirect_relationships(remove_id, keep_id)
            await self.entity_manager.delete(remove_id)

            # Remove from cached pairs
            self._duplicate_pairs = [
                p for p in self._duplicate_pairs if remove_id not in {p.entity1_id, p.entity2_id}
            ]

            log.info(
                "merge_entities_complete",
                keep_id=keep_id,
                removed_id=remove_id,
            )

            return True

        except Exception as e:
            log.exception("merge_entities_failed", error=str(e))
            return False

    async def _fetch_entities_with_embeddings(
        self,
        entity_types: list[str] | None = None,
    ) -> list[tuple[str, str, str, list[float]]]:
        """Fetch all entities that have embeddings.

        Returns:
            List of (id, name, type, embedding) tuples.
        """
        try:
            return await self._fetch_entities_with_embeddings_via_manager(entity_types)
        except Exception as e:
            log.warning("fetch_entities_with_embeddings_failed", error=str(e))
            return []

    async def _fetch_entities_with_embeddings_via_manager(
        self,
        entity_types: list[str] | None = None,
    ) -> list[tuple[str, str, str, list[float]]]:
        allowed_types = {entity_type.lower() for entity_type in entity_types or []}
        entities: list[tuple[str, str, str, list[float]]] = []
        offset = 0
        page_size = max(self.config.batch_size, 100)

        while True:
            batch = await self.entity_manager.list_all(
                limit=page_size,
                offset=offset,
                include_archived=True,
            )
            if not batch:
                break

            offset += len(batch)
            for entity in batch:
                entity_type = entity.entity_type.value
                if allowed_types and entity_type.lower() not in allowed_types:
                    continue
                if not entity.id or not isinstance(entity.embedding, list) or not entity.embedding:
                    continue
                entities.append((entity.id, entity.name, entity_type, entity.embedding))

        return entities

    async def _redirect_relationships(self, from_id: str, to_id: str) -> int:
        """Redirect all relationships from one entity to another.

        Args:
            from_id: Source entity ID (being removed).
            to_id: Target entity ID (being kept).

        Returns:
            Number of relationships redirected.
        """
        try:
            relationship_manager = self._get_relationship_manager()
            relationships = await relationship_manager.get_for_entity(from_id, direction="both")
            return await self._redirect_relationships_via_manager(
                relationship_manager,
                relationships,
                from_id,
                to_id,
            )
        except Exception as e:
            log.warning("redirect_relationships_failed", error=str(e))
            return 0

    def _get_relationship_manager(self) -> Any:
        from sibyl_core.services.graph import (
            RelationshipManager,
            SurrealGraphClient,
        )

        if not isinstance(self.client, SurrealGraphClient):
            raise RuntimeError("Entity deduplication requires a native graph client")

        return RelationshipManager(self.client, group_id=self._require_group_id())

    async def _redirect_relationships_via_manager(
        self,
        relationship_manager: Any,
        relationships: list[Any],
        from_id: str,
        to_id: str,
    ) -> int:
        from sibyl_core.models.entities import Relationship

        total_redirected = 0
        replacements: list[Relationship] = []
        previous_ids_by_replacement: dict[str, str] = {}

        for relationship in relationships:
            new_source_id = to_id if relationship.source_id == from_id else relationship.source_id
            new_target_id = to_id if relationship.target_id == from_id else relationship.target_id

            replacement = Relationship(
                id=str(uuid4()),
                relationship_type=relationship.relationship_type,
                source_id=new_source_id,
                target_id=new_target_id,
                weight=relationship.weight,
                metadata=dict(relationship.metadata or {}),
            )

            replacements.append(replacement)
            if relationship.id:
                previous_ids_by_replacement[replacement.id] = relationship.id

        try:
            created_ids = await self._create_relationships_bulk(
                relationship_manager,
                replacements,
            )
            if created_ids is not None:
                created_set = set(created_ids)
                previous_ids = [
                    previous_id
                    for replacement_id, previous_id in previous_ids_by_replacement.items()
                    if replacement_id in created_set
                ]
                deleted_count = await self._delete_relationship_ids(
                    relationship_manager,
                    previous_ids,
                )
                total_redirected = min(len(created_ids), deleted_count)
            else:
                for replacement in replacements:
                    await relationship_manager.create(replacement)
                    previous_id = previous_ids_by_replacement.get(replacement.id)
                    if previous_id:
                        await relationship_manager.delete(previous_id)
                    total_redirected += 1
        except Exception as e:
            log.warning(
                "redirect_relationships_via_manager_failed",
                from_id=from_id,
                to_id=to_id,
                relationship_count=len(relationships),
                error=str(e),
            )

        log.debug(
            "relationships_redirected",
            from_id=from_id,
            to_id=to_id,
            count=total_redirected,
            strategy="relationship_manager",
        )

        return total_redirected

    async def _create_relationships_bulk(
        self,
        relationship_manager: Any,
        relationships: list[Any],
    ) -> list[str] | None:
        create_direct_bulk = getattr(relationship_manager, "create_direct_bulk", None)
        if not callable(create_direct_bulk):
            return None
        result = create_direct_bulk(relationships)
        if inspect.isawaitable(result):
            created_ids = await result
        elif isinstance(result, list):
            created_ids = result
        else:
            return None
        return [str(created_id) for created_id in created_ids]

    async def _delete_relationship_ids(
        self,
        relationship_manager: Any,
        relationship_ids: list[str],
    ) -> int:
        if not relationship_ids:
            return 0
        delete_bulk = getattr(relationship_manager, "delete_bulk", None)
        if callable(delete_bulk):
            result = delete_bulk(relationship_ids)
            if inspect.isawaitable(result):
                return int(await result)
            if isinstance(result, int):
                return result
        deleted = 0
        for relationship_id in relationship_ids:
            if await relationship_manager.delete(relationship_id):
                deleted += 1
        return deleted

    def _suggest_keep(
        self,
        id1: str,
        id2: str,
        name1: str,
        name2: str,
    ) -> str:
        """Suggest which entity to keep based on simple heuristics.

        Prefers:
        - Longer names (more descriptive)
        - Earlier IDs (older entities)
        """
        # Prefer longer/more descriptive name
        if len(name1) > len(name2) + 5:
            return id1
        if len(name2) > len(name1) + 5:
            return id2

        # Default to first ID (arbitrary but consistent)
        return id1


# Global deduplicator instance (optional convenience)
_deduplicator: EntityDeduplicator | None = None


def get_deduplicator(
    client: Any,
    entity_manager: Any,
    config: DedupConfig | None = None,
) -> EntityDeduplicator:
    """Get or create a global deduplicator instance."""
    global _deduplicator
    if _deduplicator is None or config is not None:
        _deduplicator = EntityDeduplicator(
            client=client,
            entity_manager=entity_manager,
            config=config or DedupConfig(),
        )
    return _deduplicator


async def find_duplicates(
    client: Any,
    entity_manager: Any,
    threshold: float = 0.95,
    entity_types: list[str] | None = None,
) -> list[DuplicatePair]:
    """Convenience function to find duplicates.

    Args:
        client: Graph client.
        entity_manager: Entity manager.
        threshold: Similarity threshold.
        entity_types: Optional type filter.

    Returns:
        List of duplicate pairs.
    """
    dedup = get_deduplicator(client, entity_manager)
    return await dedup.find_duplicates(entity_types=entity_types, threshold=threshold)
