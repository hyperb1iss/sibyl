"""Entity deduplication using embedding similarity.

Detects and merges duplicate entities based on semantic similarity
of their embeddings. Redirects relationships during merge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

if TYPE_CHECKING:
    from sibyl.graph.client import GraphClient
    from sibyl.graph.entities import EntityManager

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
    """

    similarity_threshold: float = 0.95
    batch_size: int = 100
    same_type_only: bool = True
    min_name_overlap: float = 0.3


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

    client: "GraphClient"
    entity_manager: "EntityManager"
    config: DedupConfig = field(default_factory=DedupConfig)

    # Internal state
    _duplicate_pairs: list[DuplicatePair] = field(default_factory=list, init=False)

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

        # Fetch all entities with embeddings
        entities = await self._fetch_entities_with_embeddings(entity_types)

        if len(entities) < 2:
            log.info("find_duplicates_insufficient_entities", count=len(entities))
            return []

        # Compare all pairs (O(n^2) but filtered)
        pairs: list[DuplicatePair] = []
        seen_pairs: set[tuple[str, str]] = set()

        for i, (id1, name1, type1, emb1) in enumerate(entities):
            for id2, name2, type2, emb2 in entities[i + 1:]:
                # Skip if same entity
                if id1 == id2:
                    continue

                # Skip if different types (when same_type_only)
                if self.config.same_type_only and type1 != type2:
                    continue

                # Skip if already compared (in either direction)
                pair_key = tuple(sorted([id1, id2]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                # Calculate embedding similarity
                similarity = cosine_similarity(emb1, emb2)

                if similarity < similarity_threshold:
                    continue

                # Optional: check name overlap as secondary filter
                if self.config.min_name_overlap > 0:
                    name_sim = jaccard_similarity(name1, name2)
                    if name_sim < self.config.min_name_overlap:
                        continue

                # Suggest keeping the entity with more content/metadata
                suggested_keep = self._suggest_keep(id1, id2, name1, name2)

                pairs.append(
                    DuplicatePair(
                        entity1_id=id1,
                        entity2_id=id2,
                        similarity=similarity,
                        entity1_name=name1,
                        entity2_name=name2,
                        entity_type=type1,
                        suggested_keep=suggested_keep,
                    )
                )

        # Sort by similarity (highest first)
        pairs.sort(key=lambda p: p.similarity, reverse=True)

        self._duplicate_pairs = pairs

        log.info(
            "find_duplicates_complete",
            total_entities=len(entities),
            duplicate_pairs=len(pairs),
        )

        return pairs

    def suggest_merges(self) -> list[DuplicatePair]:
        """Return the current list of suggested merges.

        Call find_duplicates() first to populate this list.

        Returns:
            List of duplicate pairs with merge suggestions.
        """
        return self._duplicate_pairs

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

            # Step 1: Redirect relationships from remove -> keep
            await self._redirect_relationships(remove_id, keep_id)

            # Step 2: Optionally merge metadata
            if merge_metadata and remove_entity.metadata:
                merged_meta = {**remove_entity.metadata, **(keep_entity.metadata or {})}
                # Keep entity's metadata takes precedence
                await self.entity_manager.update(keep_id, {"metadata": merged_meta})

            # Step 3: Delete the duplicate entity
            await self.entity_manager.delete(remove_id)

            # Remove from cached pairs
            self._duplicate_pairs = [
                p for p in self._duplicate_pairs
                if p.entity1_id != remove_id and p.entity2_id != remove_id
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
        # Build Cypher query to fetch entities with embeddings
        type_filter = ""
        params: dict[str, Any] = {}

        if entity_types:
            type_filter = "AND n.entity_type IN $types"
            params["types"] = entity_types

        query = f"""
        MATCH (n:Entity)
        WHERE n.name_embedding IS NOT NULL {type_filter}
        RETURN n.uuid AS id,
               n.name AS name,
               n.entity_type AS type,
               n.name_embedding AS embedding
        """

        try:
            result = await self.client.client.driver.execute_query(query, **params)

            entities: list[tuple[str, str, str, list[float]]] = []
            for record in result:
                if isinstance(record, (list, tuple)):
                    entity_id = record[0] if len(record) > 0 else None
                    name = record[1] if len(record) > 1 else ""
                    entity_type = record[2] if len(record) > 2 else ""
                    embedding = record[3] if len(record) > 3 else None
                else:
                    entity_id = record.get("id")
                    name = record.get("name", "")
                    entity_type = record.get("type", "")
                    embedding = record.get("embedding")

                if entity_id and embedding and isinstance(embedding, list):
                    entities.append((entity_id, name, entity_type, embedding))

            return entities

        except Exception as e:
            log.warning("fetch_entities_with_embeddings_failed", error=str(e))
            return []

    async def _redirect_relationships(self, from_id: str, to_id: str) -> int:
        """Redirect all relationships from one entity to another.

        Args:
            from_id: Source entity ID (being removed).
            to_id: Target entity ID (being kept).

        Returns:
            Number of relationships redirected.
        """
        # Redirect outgoing relationships
        outgoing_query = """
        MATCH (source:Entity {uuid: $from_id})-[r]->(target)
        WHERE target.uuid <> $to_id
        WITH source, r, target, type(r) AS rel_type, properties(r) AS props
        MERGE (keep:Entity {uuid: $to_id})
        MERGE (keep)-[new_r:RELATIONSHIP]->(target)
        SET new_r = props
        DELETE r
        RETURN count(r) AS redirected
        """

        # Redirect incoming relationships
        incoming_query = """
        MATCH (source)-[r]->(target:Entity {uuid: $from_id})
        WHERE source.uuid <> $to_id
        WITH source, r, target, type(r) AS rel_type, properties(r) AS props
        MERGE (keep:Entity {uuid: $to_id})
        MERGE (source)-[new_r:RELATIONSHIP]->(keep)
        SET new_r = props
        DELETE r
        RETURN count(r) AS redirected
        """

        total_redirected = 0
        params = {"from_id": from_id, "to_id": to_id}

        try:
            # Execute both redirections
            for query in [outgoing_query, incoming_query]:
                result = await self.client.client.driver.execute_query(query, **params)
                if result:
                    for record in result:
                        if isinstance(record, (list, tuple)) and len(record) > 0:
                            total_redirected += record[0] or 0
                        elif hasattr(record, "get"):
                            total_redirected += record.get("redirected", 0)

            log.debug(
                "relationships_redirected",
                from_id=from_id,
                to_id=to_id,
                count=total_redirected,
            )

        except Exception as e:
            log.warning("redirect_relationships_failed", error=str(e))

        return total_redirected

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
    client: "GraphClient",
    entity_manager: "EntityManager",
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
    client: "GraphClient",
    entity_manager: "EntityManager",
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
