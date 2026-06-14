"""Native memory projection for source entities that contain prose."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.memory_extraction import ExtractedMemoryEntity
from sibyl_core.retrieval.dedup import DedupConfig, EntityDeduplicator
from sibyl_core.retrieval.fact_frames import FactFrame, extract_evidence_fact_frames
from sibyl_core.tools.helpers import _generate_id

log = structlog.get_logger()

PROJECTABLE_ENTITY_TYPES = frozenset({EntityType.DOCUMENT, EntityType.EPISODE, EntityType.SESSION})
DEFAULT_MAX_PROJECTED_ENTITIES = 8
DEFAULT_MAX_PROJECTED_FACTS = 6
DEFAULT_MIN_CONFIDENCE = 0.55
DEFAULT_MIN_FACT_CONFIDENCE = 0.62
_MAX_CONTEXT_CHARS = 240
_MAX_DESCRIPTION_CHARS = 360
_MAX_FACT_SPAN_CHARS = 420
_MAX_FACT_CONTENT_CHARS = 900
_FACT_TERM_LIMIT = 18
_EVENT_ACTIONS = frozenset({"acquire", "attend", "create", "repair", "use"})
_PREFERENCE_ACTIONS = frozenset({"preference"})
_FACT_ENTITY_LABELS = {
    EntityType.CLAIM: "Claim",
    EntityType.EVENT: "Event",
    EntityType.PREFERENCE: "Preference",
}

_CAPITALIZED_PHRASE_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&'.-]{2,}|[A-Z]{2,})"
    r"(?:\s+(?:[A-Z][A-Za-z0-9&'.-]{2,}|[A-Z]{2,}|[&-])){0,4}\b"
)
_QUOTED_PHRASE_RE = re.compile(r"[\"']([^\"'\n]{3,80})[\"']")
_ACTION_OBJECT_RE = re.compile(
    r"\b(?:"
    r"bought|purchased|ordered|got|picked up|visited|booked|watched|played|read|ate|drank|"
    r"use|uses|using|like|likes|liked|love|loves|prefer|prefers|need|needs|want|wants|"
    r"have|has|own|owns|met|called|named|started|joined|changed|switched to|moved to"
    r")\s+(?:a|an|the|my|our|his|her|their|some|new|another)?\s*"
    r"([A-Za-z0-9][^.;!?\n]{2,90})",
    re.IGNORECASE,
)
_RELATION_RE = re.compile(
    r"\b(?i:my|our|his|her|their)\s+"
    r"(?i:mother|father|mom|dad|sister|brother|wife|husband|partner|friend|boss|colleague|"
    r"roommate|neighbor|cousin|aunt|uncle|grandmother|grandfather)\s+"
    r"(?:(?i:is|was|named|called)\s+)?"
    r"([A-Z][A-Za-z'.-]{2,}(?:\s+[A-Z][A-Za-z'.-]{2,})?)",
)
_TRAILING_CLAUSE_RE = re.compile(
    r"\s+(?:because|when|while|after|before|for|with|from|to|at|on|in|and then|but)\b.*$",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'.-]*")
_STOPWORDS = {
    "about",
    "after",
    "also",
    "because",
    "before",
    "being",
    "could",
    "doing",
    "from",
    "have",
    "into",
    "just",
    "like",
    "more",
    "much",
    "need",
    "only",
    "over",
    "said",
    "some",
    "that",
    "their",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "today",
    "want",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
}
_CAPITALIZED_STOPWORDS = {
    "Assistant",
    "ChatGPT",
    "Human",
    "LongMemEval",
    "Question",
    "Session",
    "User",
}
_TRAILING_OBJECT_WORDS = {
    "recently",
    "today",
    "tomorrow",
    "tonight",
    "yesterday",
}
_SCOPE_METADATA_KEYS = (
    "project_id",
    "memory_scope",
    "scope_key",
    "principal_id",
    "agent_id",
    "source_id",
    "raw_source_id",
)


@dataclass(frozen=True, slots=True)
class ProjectedMemoryEntity:
    name: str
    entity_type: EntityType = EntityType.TOPIC
    description: str = ""
    context: str = ""
    confidence: float = 0.7
    extractor: str = "heuristic"
    kind: str = "mention"


@dataclass(frozen=True, slots=True)
class ProjectedMemoryFact:
    name: str
    entity_type: EntityType
    content: str
    span: str
    confidence: float
    actions: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    extractor: str = "fact_frame"
    kind: str = "memory_fact"


@dataclass(frozen=True, slots=True)
class MemoryProjectionResult:
    source_id: str
    extracted: int = 0
    projected_entities: int = 0
    relationships: int = 0
    projection_state: str = "complete"
    skipped: bool = False
    reason: str | None = None
    created_projected_entities: tuple[Entity, ...] = field(default_factory=tuple)
    created_projection_relationships: tuple[Relationship, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MemoryProjectionBatchResult:
    sources: int = 0
    extracted: int = 0
    projected_entities: int = 0
    relationships: int = 0
    projection_state: str = "complete"
    skipped: int = 0
    projected_entity_ids_by_source_id: dict[str, tuple[str, ...]] = field(default_factory=dict)
    projected_entity_links_by_source_id: dict[str, tuple[ProjectedEntitySourceLink, ...]] = field(
        default_factory=dict
    )
    created_projected_entities: tuple[Entity, ...] = field(default_factory=tuple)
    created_projection_relationships: tuple[Relationship, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ProjectedEntitySourceLink:
    entity_id: str
    name: str
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class _ProjectedEntityWriteResult:
    created_ids: list[str]
    id_map: dict[str, str]
    created_entities: tuple[Entity, ...] = field(default_factory=tuple)


def extract_projected_memory_entities(
    source: Entity,
    *,
    max_entities: int = DEFAULT_MAX_PROJECTED_ENTITIES,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[ProjectedMemoryEntity]:
    if max_entities <= 0 or source.entity_type not in PROJECTABLE_ENTITY_TYPES:
        return []

    content = _source_text(source)
    if not content:
        return []

    candidates: list[ProjectedMemoryEntity] = []
    candidates.extend(_capitalized_phrase_candidates(source, content))
    candidates.extend(_quoted_phrase_candidates(source, content))
    candidates.extend(_action_object_candidates(source, content))
    candidates.extend(_relationship_name_candidates(source, content))

    deduped: dict[str, ProjectedMemoryEntity] = {}
    for candidate in candidates:
        normalized_name = _clean_phrase(candidate.name)
        if not _valid_phrase(normalized_name):
            continue
        confidence = max(0.0, min(candidate.confidence, 1.0))
        if confidence < min_confidence:
            continue
        normalized = candidate.__class__(
            name=normalized_name,
            entity_type=candidate.entity_type,
            description=candidate.description[:_MAX_DESCRIPTION_CHARS],
            context=candidate.context[:_MAX_CONTEXT_CHARS],
            confidence=confidence,
            extractor=candidate.extractor,
            kind=candidate.kind,
        )
        key = f"{normalized.entity_type.value}:{normalized.name.lower()}"
        existing = deduped.get(key)
        if existing is None or normalized.confidence > existing.confidence:
            deduped[key] = normalized

    ordered = sorted(
        deduped.values(),
        key=lambda item: (item.confidence, len(item.name)),
        reverse=True,
    )
    return ordered[:max_entities]


def extract_projected_memory_facts(
    source: Entity,
    *,
    max_facts: int = DEFAULT_MAX_PROJECTED_FACTS,
    min_confidence: float = DEFAULT_MIN_FACT_CONFIDENCE,
) -> list[ProjectedMemoryFact]:
    if max_facts <= 0 or source.entity_type not in PROJECTABLE_ENTITY_TYPES:
        return []

    content = _source_text(source)
    if not content:
        return []

    deduped: dict[str, ProjectedMemoryFact] = {}
    for frame in extract_evidence_fact_frames(content):
        fact = _projected_fact_from_frame(source, frame)
        if fact is None or fact.confidence < min_confidence:
            continue
        key = f"{fact.entity_type.value}:{_fact_identity(fact)}"
        existing = deduped.get(key)
        if existing is None or fact.confidence > existing.confidence:
            deduped[key] = fact

    ordered = sorted(
        deduped.values(),
        key=lambda item: (item.confidence, len(item.actions), len(item.categories), len(item.span)),
        reverse=True,
    )
    return ordered[:max_facts]


async def project_memory_entity(
    *,
    entity_manager: Any,
    relationship_manager: Any,
    source: Entity,
    group_id: str,
    created_source_id: str | None = None,
    max_entities: int = DEFAULT_MAX_PROJECTED_ENTITIES,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    generate_embeddings: bool = True,
) -> MemoryProjectionResult:
    source_id = created_source_id or source.id
    batch = await project_memory_entities(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources=[source],
        group_id=group_id,
        created_source_ids=[source_id],
        max_entities=max_entities,
        min_confidence=min_confidence,
        generate_embeddings=generate_embeddings,
    )
    if batch.sources == 0:
        reason = (
            "unsupported_type"
            if source.entity_type not in PROJECTABLE_ENTITY_TYPES
            else "no_candidates"
        )
        return MemoryProjectionResult(source_id=source_id, skipped=True, reason=reason)
    return MemoryProjectionResult(
        source_id=source_id,
        extracted=batch.extracted,
        projected_entities=batch.projected_entities,
        relationships=batch.relationships,
        projection_state=batch.projection_state,
        skipped=batch.skipped > 0,
        created_projected_entities=batch.created_projected_entities,
        created_projection_relationships=batch.created_projection_relationships,
        errors=batch.errors,
    )


async def project_memory_entities(
    *,
    entity_manager: Any,
    relationship_manager: Any,
    sources: Sequence[Entity],
    group_id: str,
    created_source_ids: Sequence[str] | None = None,
    max_entities: int = DEFAULT_MAX_PROJECTED_ENTITIES,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    generate_embeddings: bool = True,
) -> MemoryProjectionBatchResult:
    now = datetime.now(UTC)
    source_ids = list(created_source_ids or [])
    projected_by_id: dict[str, Entity] = {}
    projected_entity_links_by_source_id: dict[str, tuple[ProjectedEntitySourceLink, ...]] = {}
    relationships: list[Relationship] = []
    extracted_count = 0
    skipped = 0

    for index, source in enumerate(sources):
        source_id = source_ids[index] if index < len(source_ids) else source.id
        if source.entity_type not in PROJECTABLE_ENTITY_TYPES:
            skipped += 1
            continue
        if not _projection_allowed(source):
            skipped += 1
            continue
        projected_source = source.model_copy(update={"id": source_id})
        extracted = extract_projected_memory_entities(
            projected_source,
            max_entities=max_entities,
            min_confidence=min_confidence,
        )
        extracted_facts = extract_projected_memory_facts(
            projected_source,
            max_facts=DEFAULT_MAX_PROJECTED_FACTS,
            min_confidence=max(min_confidence, DEFAULT_MIN_FACT_CONFIDENCE),
        )
        if not extracted and not extracted_facts:
            skipped += 1
            continue
        extracted_count += len(extracted) + len(extracted_facts)
        projected_links = (
            *_add_projection_candidates(
                group_id=group_id,
                now=now,
                source=projected_source,
                source_id=source_id,
                candidates=extracted,
                projected_by_id=projected_by_id,
                relationships=relationships,
            ),
            *_add_fact_candidates(
                group_id=group_id,
                now=now,
                source=projected_source,
                source_id=source_id,
                facts=extracted_facts,
                projected_by_id=projected_by_id,
                relationships=relationships,
            ),
        )
        projected_entity_links_by_source_id[source_id] = _dedupe_projected_links(projected_links)

    return await _persist_projection_batch(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources_count=len(sources),
        extracted_count=extracted_count,
        skipped=skipped,
        projected_by_id=projected_by_id,
        projected_entity_links_by_source_id=projected_entity_links_by_source_id,
        relationships=relationships,
        generate_embeddings=generate_embeddings,
    )


async def project_extracted_memory_entities(
    *,
    entity_manager: Any,
    relationship_manager: Any,
    sources: Sequence[Entity],
    extractions_by_source_id: Mapping[str, Sequence[ExtractedMemoryEntity]],
    group_id: str,
    created_source_ids: Sequence[str] | None = None,
    max_entities: int = DEFAULT_MAX_PROJECTED_ENTITIES,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    generate_embeddings: bool = True,
) -> MemoryProjectionBatchResult:
    now = datetime.now(UTC)
    source_ids = list(created_source_ids or [])
    projected_by_id: dict[str, Entity] = {}
    projected_entity_links_by_source_id: dict[str, tuple[ProjectedEntitySourceLink, ...]] = {}
    relationships: list[Relationship] = []
    extracted_count = 0
    skipped = 0

    for index, source in enumerate(sources):
        source_id = source_ids[index] if index < len(source_ids) else source.id
        if source.entity_type not in PROJECTABLE_ENTITY_TYPES:
            skipped += 1
            continue
        projected_source = source.model_copy(update={"id": source_id})
        extracted = _projected_from_extracted_entities(
            extractions_by_source_id.get(source_id, ()),
            source=projected_source,
            max_entities=max_entities,
            min_confidence=min_confidence,
        )
        if not extracted:
            skipped += 1
            continue
        extracted_count += len(extracted)
        projected_ids = _add_projection_candidates(
            group_id=group_id,
            now=now,
            source=projected_source,
            source_id=source_id,
            candidates=extracted,
            projected_by_id=projected_by_id,
            relationships=relationships,
        )
        projected_entity_links_by_source_id[source_id] = _dedupe_projected_links(projected_ids)

    return await _persist_projection_batch(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources_count=len(sources),
        extracted_count=extracted_count,
        skipped=skipped,
        projected_by_id=projected_by_id,
        projected_entity_links_by_source_id=projected_entity_links_by_source_id,
        relationships=relationships,
        generate_embeddings=generate_embeddings,
    )


async def _persist_projection_batch(
    *,
    entity_manager: Any,
    relationship_manager: Any,
    sources_count: int,
    extracted_count: int,
    skipped: int,
    projected_by_id: dict[str, Entity],
    projected_entity_links_by_source_id: Mapping[str, Sequence[ProjectedEntitySourceLink]],
    relationships: list[Relationship],
    generate_embeddings: bool,
) -> MemoryProjectionBatchResult:
    if not projected_by_id:
        return MemoryProjectionBatchResult(sources=0, skipped=skipped)

    errors: list[str] = []
    entity_writes: _ProjectedEntityWriteResult
    try:
        entities_to_create = await _missing_projected_entities(
            entity_manager, list(projected_by_id.values())
        )
        entity_writes = await _create_projected_entities(
            entity_manager,
            entities_to_create,
            generate_embeddings=generate_embeddings,
        )
    except Exception as exc:
        log.warning(
            "memory_projection_entities_failed",
            sources=sources_count,
            error_type=type(exc).__name__,
        )
        return MemoryProjectionBatchResult(
            sources=sources_count,
            extracted=extracted_count,
            skipped=skipped,
            projection_state="partial",
            errors=(str(exc),),
        )

    resolved_links_by_source_id = {
        source_id: _dedupe_projected_links(
            ProjectedEntitySourceLink(
                entity_id=entity_writes.id_map.get(link.entity_id, link.entity_id),
                name=link.name,
                evidence=link.evidence,
            )
            for link in links
        )
        for source_id, links in projected_entity_links_by_source_id.items()
    }
    resolved_entity_ids_by_source_id = {
        source_id: tuple(link.entity_id for link in links)
        for source_id, links in resolved_links_by_source_id.items()
    }
    relationship_count = 0
    created_projection_relationships: tuple[Relationship, ...] = ()
    if relationships:
        try:
            resolved_relationships = _relationships_with_resolved_entity_ids(
                relationships,
                entity_writes.id_map,
            )
            create_direct_bulk = getattr(relationship_manager, "create_direct_bulk", None)
            if callable(create_direct_bulk):
                created_relationship_ids = list(
                    await create_direct_bulk(
                        resolved_relationships,
                        generate_embeddings=generate_embeddings,
                    )
                )
                relationship_count = len(created_relationship_ids)
                created_relationship_id_set = set(created_relationship_ids)
                created_projection_relationships = tuple(
                    relationship
                    for relationship in resolved_relationships
                    if relationship.id in created_relationship_id_set
                )
                failed = len(resolved_relationships) - relationship_count
            else:
                created, failed = await relationship_manager.create_bulk(resolved_relationships)
                relationship_count = created
                created_projection_relationships = tuple(resolved_relationships[:created])
            if failed:
                errors.append(f"{failed} projection relationships failed")
        except Exception as exc:
            log.warning(
                "memory_projection_relationships_failed",
                sources=sources_count,
                error_type=type(exc).__name__,
            )
            errors.append(str(exc))

    return MemoryProjectionBatchResult(
        sources=sources_count,
        extracted=extracted_count,
        projected_entities=len(entity_writes.created_ids),
        relationships=relationship_count,
        projection_state="partial" if errors else "complete",
        skipped=skipped,
        projected_entity_ids_by_source_id=resolved_entity_ids_by_source_id,
        projected_entity_links_by_source_id=resolved_links_by_source_id,
        created_projected_entities=entity_writes.created_entities,
        created_projection_relationships=created_projection_relationships,
        errors=tuple(errors),
    )


async def _create_projected_entities(
    entity_manager: Any,
    entities: Sequence[Entity],
    *,
    generate_embeddings: bool,
) -> _ProjectedEntityWriteResult:
    id_map = {entity.id: entity.id for entity in entities}
    if not entities:
        return _ProjectedEntityWriteResult(created_ids=[], id_map=id_map)

    prepared_entities, prepared_for_write = await _prepare_projected_entities(
        entity_manager,
        entities,
        generate_embeddings=generate_embeddings,
    )
    generate_on_create = generate_embeddings and not prepared_for_write
    resolved = await _resolve_projected_entities(entity_manager, prepared_entities)
    unresolved_entities: list[Entity] = []
    for entity in prepared_entities:
        resolved_id = resolved.get(entity.id)
        if resolved_id:
            id_map[entity.id] = resolved_id
        else:
            unresolved_entities.append(entity)
    if not unresolved_entities:
        return _ProjectedEntityWriteResult(created_ids=[], id_map=id_map)

    create_direct_bulk = getattr(entity_manager, "create_direct_bulk", None)
    if callable(create_direct_bulk):
        created_ids = list(
            await create_direct_bulk(
                unresolved_entities,
                generate_embeddings=generate_on_create,
            )
        )
        return _ProjectedEntityWriteResult(
            created_ids=created_ids,
            id_map=id_map,
            created_entities=tuple(
                entity.model_copy(update={"id": created_id})
                for entity, created_id in zip(unresolved_entities, created_ids, strict=False)
            ),
        )

    bulk_create_direct = getattr(entity_manager, "bulk_create_direct", None)
    if callable(bulk_create_direct):
        created, failed = await bulk_create_direct(unresolved_entities)
        if failed:
            raise RuntimeError(f"{failed} projected entities failed to persist")
        if created != len(unresolved_entities):
            raise RuntimeError(
                f"projected entity count mismatch: {created}/{len(unresolved_entities)}"
            )
        return _ProjectedEntityWriteResult(
            created_ids=[entity.id for entity in unresolved_entities],
            id_map=id_map,
            created_entities=tuple(unresolved_entities),
        )

    create_direct = getattr(entity_manager, "create_direct", None)
    if callable(create_direct):
        created_ids: list[str] = []
        created_entities: list[Entity] = []
        for entity in unresolved_entities:
            created_id = await create_direct(entity, generate_embedding=generate_on_create)
            created_ids.append(created_id)
            created_entities.append(entity.model_copy(update={"id": created_id}))
        return _ProjectedEntityWriteResult(
            created_ids=created_ids,
            id_map=id_map,
            created_entities=tuple(created_entities),
        )

    created_ids = []
    created_entities = []
    for entity in unresolved_entities:
        created_id = await entity_manager.create(entity)
        created_ids.append(created_id)
        created_entities.append(entity.model_copy(update={"id": created_id}))
    return _ProjectedEntityWriteResult(
        created_ids=created_ids,
        id_map=id_map,
        created_entities=tuple(created_entities),
    )


async def _prepare_projected_entities(
    entity_manager: Any,
    entities: Sequence[Entity],
    *,
    generate_embeddings: bool,
) -> tuple[list[Entity], bool]:
    prepare_entities = getattr(entity_manager, "prepare_entities_for_write", None)
    if callable(prepare_entities):
        return (
            list(
                await prepare_entities(
                    list(entities),
                    generate_embeddings=generate_embeddings,
                )
            ),
            True,
        )
    return list(entities), False


async def _resolve_projected_entities(
    entity_manager: Any,
    entities: Sequence[Entity],
) -> dict[str, str]:
    if not any(entity.embedding for entity in entities):
        return {}
    client = getattr(entity_manager, "_client", None)
    if client is None:
        return {}
    deduplicator = EntityDeduplicator(
        client=client,
        entity_manager=entity_manager,
        config=DedupConfig(
            similarity_threshold=0.95,
            same_type_only=True,
            scope_metadata_keys=(
                "memory_scope",
                "scope_key",
                "project_id",
                "principal_id",
                "agent_id",
            ),
        ),
    )
    matches = await deduplicator.resolve_existing_entities(entities)
    return {entity_id: pair.entity2_id for entity_id, pair in matches.items()}


def _relationships_with_resolved_entity_ids(
    relationships: Sequence[Relationship],
    id_map: Mapping[str, str],
) -> list[Relationship]:
    remapped: dict[str, Relationship] = {}
    for relationship in relationships:
        source_id = id_map.get(relationship.source_id, relationship.source_id)
        target_id = id_map.get(relationship.target_id, relationship.target_id)
        if source_id == relationship.source_id and target_id == relationship.target_id:
            replacement = relationship
        else:
            replacement = relationship.model_copy(
                update={
                    "id": _generate_id(
                        "rel",
                        source_id,
                        relationship.relationship_type.value,
                        target_id,
                    ),
                    "source_id": source_id,
                    "target_id": target_id,
                }
            )
        remapped[replacement.id] = replacement
    return list(remapped.values())


def _add_projection_candidates(
    *,
    group_id: str,
    now: datetime,
    source: Entity,
    source_id: str,
    candidates: Sequence[ProjectedMemoryEntity],
    projected_by_id: dict[str, Entity],
    relationships: list[Relationship],
) -> list[ProjectedEntitySourceLink]:
    projected_links: list[ProjectedEntitySourceLink] = []
    for candidate in candidates:
        entity = _projected_entity(
            candidate,
            group_id=group_id,
            now=now,
            source=source,
        )
        projected_by_id.setdefault(entity.id, entity)
        projected_links.append(
            ProjectedEntitySourceLink(
                entity_id=entity.id,
                name=candidate.name,
                evidence=candidate.context,
            )
        )
        relationships.append(
            _projection_relationship(
                source=source,
                source_id=source_id,
                target_id=entity.id,
                candidate=candidate,
                now=now,
            )
        )
    return projected_links


def _add_fact_candidates(
    *,
    group_id: str,
    now: datetime,
    source: Entity,
    source_id: str,
    facts: Sequence[ProjectedMemoryFact],
    projected_by_id: dict[str, Entity],
    relationships: list[Relationship],
) -> list[ProjectedEntitySourceLink]:
    projected_links: list[ProjectedEntitySourceLink] = []
    for fact in facts:
        entity = _projected_fact_entity(
            fact,
            group_id=group_id,
            now=now,
            source=source,
        )
        projected_by_id.setdefault(entity.id, entity)
        projected_links.append(
            ProjectedEntitySourceLink(
                entity_id=entity.id,
                name=fact.name,
                evidence=fact.span,
            )
        )
        relationships.append(
            _fact_projection_relationship(
                source=source,
                source_id=source_id,
                target_id=entity.id,
                fact=fact,
                now=now,
            )
        )
    return projected_links


def _dedupe_projected_links(
    links: Iterable[ProjectedEntitySourceLink],
) -> tuple[ProjectedEntitySourceLink, ...]:
    by_id: dict[str, ProjectedEntitySourceLink] = {}
    for link in links:
        existing = by_id.get(link.entity_id)
        if existing is None or (not existing.evidence and link.evidence):
            by_id[link.entity_id] = link
    return tuple(by_id.values())


def _projected_from_extracted_entities(
    extracted_entities: Sequence[ExtractedMemoryEntity],
    *,
    source: Entity,
    max_entities: int,
    min_confidence: float,
) -> list[ProjectedMemoryEntity]:
    if max_entities <= 0:
        return []

    deduped: dict[str, ProjectedMemoryEntity] = {}
    for extracted in extracted_entities:
        name = _clean_extracted_name(extracted.name)
        if not _valid_extracted_name(name):
            continue
        confidence = 0.75 if extracted.confidence is None else extracted.confidence
        confidence = max(0.0, min(confidence, 1.0))
        if confidence < min_confidence:
            continue
        candidate = ProjectedMemoryEntity(
            name=name,
            entity_type=extracted.to_entity_type(),
            description=(extracted.summary or f"{name} mentioned in {source.name}")[
                :_MAX_DESCRIPTION_CHARS
            ],
            context=extracted.evidence[:_MAX_CONTEXT_CHARS],
            confidence=confidence,
            extractor="llm",
            kind="llm_mention",
        )
        key = f"{candidate.entity_type.value}:{candidate.name.lower()}"
        existing = deduped.get(key)
        if existing is None or candidate.confidence > existing.confidence:
            deduped[key] = candidate

    ordered = sorted(
        deduped.values(),
        key=lambda item: (item.confidence, len(item.name)),
        reverse=True,
    )
    return ordered[:max_entities]


async def _missing_projected_entities(
    entity_manager: Any,
    entities: Sequence[Entity],
) -> list[Entity]:
    get_entity = getattr(entity_manager, "get", None)
    if not callable(get_entity):
        return list(entities)

    missing: list[Entity] = []
    for entity in entities:
        try:
            existing = await get_entity(entity.id)
        except (EntityNotFoundError, KeyError):
            missing.append(entity)
            continue
        if existing is None:
            missing.append(entity)
    return missing


def _projected_entity(
    candidate: ProjectedMemoryEntity,
    *,
    group_id: str,
    now: datetime,
    source: Entity,
) -> Entity:
    entity_id = _generate_id(
        candidate.entity_type.value,
        candidate.name,
        "memory_projection",
        group_id,
        _projection_identity_scope(source),
    )
    content = (
        "\n".join(part for part in (candidate.description, candidate.context) if part)
        or candidate.name
    )
    metadata = {
        "category": "memory_projection",
        "tags": ["projected", candidate.kind],
        "organization_id": group_id,
        "source_entity_id": source.id,
        "projection_extractor": candidate.extractor,
        "projection_kind": candidate.kind,
        "projection_confidence": candidate.confidence,
        "source_entity_type": source.entity_type.value,
        **_inherited_scope_metadata(source),
    }
    if metadata.get("memory_scope") == "project" and metadata.get("scope_key"):
        metadata["project_id"] = metadata["scope_key"]
    return Entity(
        id=entity_id,
        entity_type=candidate.entity_type,
        name=candidate.name,
        description=candidate.description or candidate.name,
        content=content,
        organization_id=group_id,
        metadata=metadata,
        created_at=now,
        updated_at=now,
    )


def _projected_fact_entity(
    fact: ProjectedMemoryFact,
    *,
    group_id: str,
    now: datetime,
    source: Entity,
) -> Entity:
    entity_id = _generate_id(
        fact.entity_type.value,
        "memory_fact",
        group_id,
        _projection_identity_scope(source),
        source.id,
        _fact_identity(fact),
    )
    tags = [
        "projected",
        "memory_fact",
        fact.kind,
        *(f"action:{action}" for action in fact.actions),
        *(f"category:{category}" for category in fact.categories),
        *(f"relation:{relation}" for relation in fact.relations),
    ]
    metadata: dict[str, object] = {
        "category": "memory_fact_projection",
        "tags": tags,
        "organization_id": group_id,
        "source_entity_type": source.entity_type.value,
        "projection_extractor": fact.extractor,
        "projection_kind": fact.kind,
        "projection_confidence": fact.confidence,
        "fact_actions": list(fact.actions),
        "fact_categories": list(fact.categories),
        "fact_relations": list(fact.relations),
        "fact_terms": list(fact.terms),
        "fact_span": fact.span,
        **_inherited_scope_metadata(source),
    }
    metadata["source_entity_id"] = source.id
    valid_at = (source.metadata or {}).get("valid_at") or (source.metadata or {}).get("valid_from")
    if valid_at is not None:
        metadata["valid_at"] = valid_at
    if metadata.get("memory_scope") == "project" and metadata.get("scope_key"):
        metadata["project_id"] = metadata["scope_key"]
    return Entity(
        id=entity_id,
        entity_type=fact.entity_type,
        name=fact.name,
        description=fact.span[:_MAX_DESCRIPTION_CHARS],
        content=fact.content,
        organization_id=group_id,
        metadata=metadata,
        created_at=now,
        updated_at=now,
    )


def _projection_relationship(
    *,
    source: Entity,
    source_id: str,
    target_id: str,
    candidate: ProjectedMemoryEntity,
    now: datetime,
) -> Relationship:
    rel_id = _generate_id("rel", source_id, RelationshipType.MENTIONS.value, target_id)
    fact = _relationship_fact(source, candidate)
    metadata = {
        "created_at": now.isoformat(),
        "auto_projected": True,
        "projection_extractor": candidate.extractor,
        "projection_kind": candidate.kind,
        "projection_confidence": candidate.confidence,
        "fact": fact,
        "episodes": [source_id],
        **_inherited_scope_metadata(source),
    }
    valid_at = (source.metadata or {}).get("valid_at") or (source.metadata or {}).get("valid_from")
    if valid_at is not None:
        metadata["valid_at"] = valid_at
    return Relationship(
        id=rel_id,
        source_id=source_id,
        target_id=target_id,
        relationship_type=RelationshipType.MENTIONS,
        weight=max(0.1, min(candidate.confidence, 1.0)),
        metadata=metadata,
        created_at=now,
    )


def _fact_projection_relationship(
    *,
    source: Entity,
    source_id: str,
    target_id: str,
    fact: ProjectedMemoryFact,
    now: datetime,
) -> Relationship:
    rel_id = _generate_id("rel", source_id, RelationshipType.MENTIONS.value, target_id)
    metadata: dict[str, object] = {
        "created_at": now.isoformat(),
        "auto_projected": True,
        "projection_extractor": fact.extractor,
        "projection_kind": fact.kind,
        "projection_confidence": fact.confidence,
        "fact": fact.span,
        "episodes": [source_id],
        "fact_actions": list(fact.actions),
        "fact_categories": list(fact.categories),
        "fact_relations": list(fact.relations),
        "fact_terms": list(fact.terms),
        **_inherited_scope_metadata(source),
    }
    valid_at = (source.metadata or {}).get("valid_at") or (source.metadata or {}).get("valid_from")
    if valid_at is not None:
        metadata["valid_at"] = valid_at
    return Relationship(
        id=rel_id,
        source_id=source_id,
        target_id=target_id,
        relationship_type=RelationshipType.MENTIONS,
        weight=max(0.1, min(fact.confidence, 1.0)),
        metadata=metadata,
        created_at=now,
    )


def _relationship_fact(source: Entity, candidate: ProjectedMemoryEntity) -> str:
    source_name = source.name or source.id
    if candidate.context:
        return f"{source_name} mentions {candidate.name}: {candidate.context}"
    return f"{source_name} mentions {candidate.name}"


def _source_text(source: Entity) -> str:
    body = " ".join(part for part in (source.description, source.content) if part)
    return body or source.name


def _projected_fact_from_frame(
    source: Entity,
    frame: FactFrame,
) -> ProjectedMemoryFact | None:
    span = " ".join(frame.span.split())[:_MAX_FACT_SPAN_CHARS]
    if not _valid_fact_span(span):
        return None

    actions = tuple(sorted(frame.actions))
    categories = tuple(sorted(frame.categories))
    relations = tuple(sorted(frame.relations))
    terms = _ordered_fact_terms(span, frame.terms)
    entity_type = _fact_entity_type(frame)
    confidence = _fact_confidence(frame)
    name = _fact_name(entity_type, span)
    content = _fact_content(
        source=source,
        span=span,
        actions=actions,
        categories=categories,
        relations=relations,
        terms=terms,
    )
    return ProjectedMemoryFact(
        name=name,
        entity_type=entity_type,
        content=content,
        span=span,
        confidence=confidence,
        actions=actions,
        categories=categories,
        relations=relations,
        terms=terms,
    )


def _fact_entity_type(frame: FactFrame) -> EntityType:
    if frame.actions & _PREFERENCE_ACTIONS:
        return EntityType.PREFERENCE
    if frame.actions & _EVENT_ACTIONS:
        return EntityType.EVENT
    return EntityType.CLAIM


def _fact_confidence(frame: FactFrame) -> float:
    confidence = 0.5
    if frame.personal:
        confidence += 0.08
    if frame.actions:
        confidence += 0.06
    if frame.categories:
        confidence += 0.05
    if frame.relations:
        confidence += 0.04
    if "recency" in frame.relations:
        confidence += 0.03
    return min(confidence, 0.9)


def _ordered_fact_terms(span: str, terms: frozenset[str]) -> tuple[str, ...]:
    remaining = set(terms)
    ordered: list[str] = []
    for token in _WORD_RE.findall(span.lower()):
        normalized = token.strip("'\"")
        if normalized.endswith("'s"):
            normalized = normalized[:-2]
        if normalized in remaining:
            ordered.append(normalized)
            remaining.remove(normalized)
        if len(ordered) >= _FACT_TERM_LIMIT:
            return tuple(ordered)
    ordered.extend(sorted(remaining))
    return tuple(ordered[:_FACT_TERM_LIMIT])


def _fact_name(entity_type: EntityType, span: str) -> str:
    label = _FACT_ENTITY_LABELS.get(entity_type, "Fact")
    cleaned = re.sub(r"^(?:User|Assistant|Human):\s*", "", span).strip()
    phrase = _trim_phrase_words(cleaned, max_words=10)
    if not phrase:
        phrase = "memory evidence"
    return f"{label}: {phrase}"[:120].rstrip()


def _fact_content(
    *,
    source: Entity,
    span: str,
    actions: Sequence[str],
    categories: Sequence[str],
    relations: Sequence[str],
    terms: Sequence[str],
) -> str:
    parts = [f"Evidence: {span}", f"Source: {source.name or source.id}"]
    if actions:
        parts.append(f"Actions: {', '.join(actions)}")
    if categories:
        parts.append(f"Categories: {', '.join(categories)}")
    if relations:
        parts.append(f"Relations: {', '.join(relations)}")
    if terms:
        parts.append(f"Terms: {', '.join(terms)}")
    valid_at = (source.metadata or {}).get("valid_at") or (source.metadata or {}).get("valid_from")
    if valid_at is not None:
        parts.append(f"Valid at: {valid_at}")
    return "\n".join(parts)[:_MAX_FACT_CONTENT_CHARS]


def _fact_identity(fact: ProjectedMemoryFact) -> str:
    return "|".join(
        (
            fact.entity_type.value,
            ",".join(fact.actions),
            ",".join(fact.categories),
            ",".join(fact.relations),
            " ".join(fact.terms),
            fact.span.lower(),
        )
    )


def _valid_fact_span(span: str) -> bool:
    words = _WORD_RE.findall(span)
    return 4 <= len(words) <= 90


def _inherited_scope_metadata(source: Entity) -> dict[str, object]:
    metadata = dict(source.metadata or {})
    return {
        key: metadata[key]
        for key in _SCOPE_METADATA_KEYS
        if key in metadata and metadata[key] is not None
    }


def _projection_allowed(source: Entity) -> bool:
    metadata = dict(source.metadata or {})
    memory_scope = str(metadata.get("memory_scope") or "org").strip().lower()
    return memory_scope not in {"private", "delegated"}


def _projection_identity_scope(source: Entity) -> str:
    metadata = dict(source.metadata or {})
    memory_scope = str(metadata.get("memory_scope") or "org").strip().lower()
    scope_key: object = metadata.get("scope_key")
    if not scope_key and memory_scope == "private":
        scope_key = metadata.get("principal_id") or source.created_by or source.id
    if not scope_key:
        scope_key = metadata.get("project_id") or metadata.get("agent_id") or "default"
    return f"{memory_scope}:{scope_key}"


def _capitalized_phrase_candidates(source: Entity, content: str) -> list[ProjectedMemoryEntity]:
    candidates: list[ProjectedMemoryEntity] = []
    for match in _CAPITALIZED_PHRASE_RE.finditer(content):
        phrase = match.group(0)
        if phrase in _CAPITALIZED_STOPWORDS:
            continue
        candidates.append(
            ProjectedMemoryEntity(
                name=phrase,
                description=f"{phrase} mentioned in {source.name}",
                context=_context(content, match.start(), match.end()),
                confidence=0.78,
                kind="proper_noun",
            )
        )
    return candidates


def _quoted_phrase_candidates(source: Entity, content: str) -> list[ProjectedMemoryEntity]:
    candidates: list[ProjectedMemoryEntity] = []
    for match in _QUOTED_PHRASE_RE.finditer(content):
        phrase = match.group(1)
        candidates.append(
            ProjectedMemoryEntity(
                name=phrase,
                description=f"{phrase} mentioned in {source.name}",
                context=_context(content, match.start(), match.end()),
                confidence=0.72,
                kind="quoted_phrase",
            )
        )
    return candidates


def _action_object_candidates(source: Entity, content: str) -> list[ProjectedMemoryEntity]:
    candidates: list[ProjectedMemoryEntity] = []
    for match in _ACTION_OBJECT_RE.finditer(content):
        phrase = _TRAILING_CLAUSE_RE.sub("", match.group(1)).strip(" ,:;-")
        phrase = _trim_phrase_words(phrase, max_words=6)
        candidates.append(
            ProjectedMemoryEntity(
                name=phrase,
                description=f"{phrase} is an object or preference mentioned in {source.name}",
                context=_context(content, match.start(), match.end()),
                confidence=0.68,
                kind="action_object",
            )
        )
    return candidates


def _relationship_name_candidates(source: Entity, content: str) -> list[ProjectedMemoryEntity]:
    candidates: list[ProjectedMemoryEntity] = []
    for match in _RELATION_RE.finditer(content):
        phrase = match.group(1)
        candidates.append(
            ProjectedMemoryEntity(
                name=phrase,
                entity_type=EntityType.CLAIM,
                description=f"{phrase} is named in a relationship mentioned in {source.name}",
                context=_context(content, match.start(), match.end()),
                confidence=0.74,
                kind="relationship_name",
            )
        )
    return candidates


def _context(content: str, start: int, end: int) -> str:
    context_start = max(0, start - 90)
    context_end = min(len(content), end + 90)
    return " ".join(content[context_start:context_end].split())


def _trim_phrase_words(phrase: str, *, max_words: int) -> str:
    words = _WORD_RE.findall(phrase)
    while words and words[-1].lower() in _TRAILING_OBJECT_WORDS:
        words.pop()
    return " ".join(words[:max_words])


def _clean_phrase(phrase: str) -> str:
    cleaned = " ".join(_WORD_RE.findall(phrase.replace("_", " ")))
    return cleaned.strip(" .,:;!?-")


def _clean_extracted_name(name: str) -> str:
    return " ".join(name.replace("_", " ").split()).strip(" .,:;!?-")


def _valid_phrase(phrase: str) -> bool:
    if len(phrase) < 3 or len(phrase) > 80:
        return False
    words = [word.lower().strip("'.-") for word in phrase.split()]
    if not words or all(word in _STOPWORDS for word in words):
        return False
    if len(words) > 8:
        return False
    return not (Path(phrase).suffix and "/" in phrase)


def _valid_extracted_name(name: str) -> bool:
    if len(name) < 2 or len(name) > 120:
        return False
    words = [word.lower().strip("'.-") for word in _WORD_RE.findall(name)]
    if not words or all(word in _STOPWORDS for word in words):
        return False
    if len(words) > 12:
        return False
    return not (Path(name).suffix and "/" in name)
