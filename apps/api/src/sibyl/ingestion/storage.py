"""Storage connector for persisting ingestion results to the knowledge graph."""

import hashlib
import re
from dataclasses import dataclass

import structlog

from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.ingestion.extractor import ExtractedEntity
from sibyl.ingestion.relationships import ExtractedRelationship, RelationType
from sibyl.models.entities import Entity, Relationship, RelationshipType

log = structlog.get_logger()


def _sanitize_name(name: str) -> str:
    """Sanitize entity name for FalkorDB/RediSearch compatibility.

    Removes or replaces characters that cause RediSearch query syntax errors.

    Args:
        name: The raw entity name.

    Returns:
        Sanitized name safe for graph storage and search.
    """
    # First pass: remove markdown formatting (bold/italic markers)
    sanitized = re.sub(r"\*{1,3}", "", name)  # Remove *, **, ***
    sanitized = re.sub(r"_{1,3}", "", sanitized)  # Remove _, __, ___

    # Second pass: remove special characters that break RediSearch
    # Includes backticks, brackets, punctuation, and quotes
    sanitized = re.sub(r"[`\[\]{}()|@#$%^&+=<>\"']", "", sanitized)

    # Replace colons and slashes with spaces (field separators)
    sanitized = sanitized.replace(":", " ").replace("/", " ")

    # Collapse multiple spaces and strip
    sanitized = re.sub(r"\s+", " ", sanitized).strip()

    # Fallback if nothing remains
    if not sanitized:
        sanitized = "unnamed_entity"

    return sanitized


# Mapping from ingestion RelationType to model RelationshipType
RELATION_TYPE_MAP: dict[RelationType, RelationshipType] = {
    RelationType.APPLIES_TO: RelationshipType.APPLIES_TO,
    RelationType.REQUIRES: RelationshipType.REQUIRES,
    RelationType.CONFLICTS_WITH: RelationshipType.CONFLICTS_WITH,
    RelationType.SUPERSEDES: RelationshipType.SUPERSEDES,
    RelationType.DOCUMENTED_IN: RelationshipType.DOCUMENTED_IN,
    RelationType.RELATED_TO: RelationshipType.RELATED_TO,
    RelationType.PART_OF: RelationshipType.PART_OF,
    RelationType.ENABLES: RelationshipType.ENABLES,
    RelationType.WARNS_ABOUT: RelationshipType.RELATED_TO,  # Map to generic
}


@dataclass
class StorageResult:
    """Result of storing data to the graph."""

    entities_stored: int
    relationships_stored: int
    entities_skipped: int
    relationships_skipped: int
    errors: list[str]


def _generate_entity_id(entity: ExtractedEntity) -> str:
    """Generate a deterministic ID for an extracted entity.

    Args:
        entity: The extracted entity.

    Returns:
        A unique, deterministic ID.
    """
    # Use entity type + name + source for uniqueness
    key = f"{entity.entity_type.value}:{entity.name.lower()}:{entity.source_episode_id}"
    hash_bytes = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"{entity.entity_type.value}_{hash_bytes}"


def _generate_relationship_id(rel: ExtractedRelationship) -> str:
    """Generate a deterministic ID for a relationship.

    Args:
        rel: The extracted relationship.

    Returns:
        A unique, deterministic ID.
    """
    key = f"{rel.source_name}:{rel.relation_type.value}:{rel.target_name}"
    hash_bytes = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"rel_{hash_bytes}"


def convert_extracted_entity(extracted: ExtractedEntity) -> Entity:
    """Convert an ExtractedEntity to the graph Entity model.

    Args:
        extracted: The extracted entity from the ingestion pipeline.

    Returns:
        Entity model ready for storage.
    """
    entity_id = _generate_entity_id(extracted)
    entity_type = extracted.to_entity_type()

    # Sanitize name and description for graph storage
    sanitized_name = _sanitize_name(extracted.name)
    sanitized_description = _sanitize_name(extracted.description) if extracted.description else ""

    return Entity(
        id=entity_id,
        entity_type=entity_type,
        name=sanitized_name,
        description=sanitized_description,
        content=extracted.context,
        metadata={
            "confidence": extracted.confidence,
            "source_episode_id": extracted.source_episode_id,
            "original_type": extracted.entity_type.value,
            "original_name": extracted.name,  # Keep original for reference
            **extracted.metadata,
        },
    )


def convert_extracted_relationship(
    rel: ExtractedRelationship,
    entity_id_map: dict[str, str],
) -> Relationship | None:
    """Convert an ExtractedRelationship to the graph Relationship model.

    Args:
        rel: The extracted relationship.
        entity_id_map: Mapping from entity names to IDs.

    Returns:
        Relationship model ready for storage, or None if source/target not found.
    """
    # Look up source and target entity IDs
    source_id = entity_id_map.get(rel.source_name.lower())
    target_id = entity_id_map.get(rel.target_name.lower())

    if not source_id or not target_id:
        log.debug(
            "Skipping relationship - missing entity",
            source=rel.source_name,
            target=rel.target_name,
            source_found=bool(source_id),
            target_found=bool(target_id),
        )
        return None

    rel_id = _generate_relationship_id(rel)
    rel_type = RELATION_TYPE_MAP.get(rel.relation_type, RelationshipType.RELATED_TO)

    return Relationship(
        id=rel_id,
        relationship_type=rel_type,
        source_id=source_id,
        target_id=target_id,
        weight=rel.confidence,
        metadata={
            "evidence": rel.evidence,
            "source_episode_id": rel.source_episode_id,
        },
    )


async def store_entities(
    entities: list[ExtractedEntity],
    *,
    group_id: str,
) -> tuple[dict[str, str], list[str]]:
    """Store extracted entities to the graph.

    Args:
        entities: List of extracted entities.
        group_id: Organization ID for multi-tenant graph operations.

    Returns:
        Tuple of (entity_name_to_id_map, errors).
    """
    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=group_id)

    entity_id_map: dict[str, str] = {}
    errors: list[str] = []
    stored_count = 0

    for extracted in entities:
        try:
            entity = convert_extracted_entity(extracted)

            # Check for duplicates by name (case-insensitive)
            name_key = entity.name.lower()
            if name_key in entity_id_map:
                # Skip duplicate, keep first occurrence
                continue
            created_id = await entity_manager.create(entity)
            entity_id_map[name_key] = created_id
            stored_count += 1

        except Exception as e:
            error_msg = f"Failed to store entity '{extracted.name}': {e}"
            log.warning(error_msg)
            errors.append(error_msg)

    log.info("Stored entities", count=stored_count, duplicates_skipped=len(entities) - stored_count)
    return entity_id_map, errors


async def store_relationships(
    relationships: list[ExtractedRelationship],
    entity_id_map: dict[str, str],
    *,
    group_id: str,
) -> tuple[int, int, list[str]]:
    """Store extracted relationships to the graph.

    Args:
        relationships: List of extracted relationships.
        entity_id_map: Mapping from entity names to IDs.
        group_id: Organization ID for multi-tenant graph operations.

    Returns:
        Tuple of (stored_count, skipped_count, errors).
    """
    client = await get_graph_client()
    relationship_manager = RelationshipManager(client, group_id=group_id)

    stored = 0
    skipped = 0
    errors: list[str] = []
    seen_rels: set[str] = set()

    for extracted in relationships:
        try:
            rel = convert_extracted_relationship(extracted, entity_id_map)
            if rel is None:
                skipped += 1
                continue

            # Skip duplicate relationships
            rel_key = f"{rel.source_id}:{rel.relationship_type}:{rel.target_id}"
            if rel_key in seen_rels:
                skipped += 1
                continue
            seen_rels.add(rel_key)

            await relationship_manager.create(rel)
            stored += 1

        except Exception as e:
            error_msg = f"Failed to store relationship {extracted.source_name}->{extracted.target_name}: {e}"
            log.warning(error_msg)
            errors.append(error_msg)

    log.info("Stored relationships", stored=stored, skipped=skipped, errors=len(errors))
    return stored, skipped, errors


async def store_ingestion_results(
    entities: list[ExtractedEntity],
    relationships: list[ExtractedRelationship],
    *,
    group_id: str,
) -> StorageResult:
    """Store all ingestion results to the knowledge graph.

    Args:
        entities: Extracted entities to store.
        relationships: Extracted relationships to store.
        group_id: Organization ID for multi-tenant graph operations.

    Returns:
        StorageResult with counts and errors.
    """
    log.info(
        "Storing ingestion results to graph",
        entities=len(entities),
        relationships=len(relationships),
    )

    all_errors: list[str] = []

    # Store entities first to build ID map
    entity_id_map, entity_errors = await store_entities(entities, group_id=group_id)
    all_errors.extend(entity_errors)

    # Store relationships using the ID map
    rels_stored, rels_skipped, rel_errors = await store_relationships(
        relationships, entity_id_map, group_id=group_id
    )
    all_errors.extend(rel_errors)

    result = StorageResult(
        entities_stored=len(entity_id_map),
        relationships_stored=rels_stored,
        entities_skipped=len(entities) - len(entity_id_map),
        relationships_skipped=rels_skipped,
        errors=all_errors,
    )

    log.info(
        "Storage complete",
        entities_stored=result.entities_stored,
        relationships_stored=result.relationships_stored,
        errors=len(result.errors),
    )

    return result
