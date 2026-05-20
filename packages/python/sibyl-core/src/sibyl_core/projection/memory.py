"""Native memory projection for source entities that contain prose."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.memory_extraction import ExtractedMemoryEntity
from sibyl_core.tools.helpers import _generate_id

log = structlog.get_logger()

PROJECTABLE_ENTITY_TYPES = frozenset({EntityType.EPISODE, EntityType.SESSION})
DEFAULT_MAX_PROJECTED_ENTITIES = 8
DEFAULT_MIN_CONFIDENCE = 0.55
_MAX_CONTEXT_CHARS = 240
_MAX_DESCRIPTION_CHARS = 360

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
class MemoryProjectionResult:
    source_id: str
    extracted: int = 0
    projected_entities: int = 0
    relationships: int = 0
    skipped: bool = False
    reason: str | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MemoryProjectionBatchResult:
    sources: int = 0
    extracted: int = 0
    projected_entities: int = 0
    relationships: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


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
        skipped=batch.skipped > 0,
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
    relationships: list[Relationship] = []
    extracted_count = 0
    skipped = 0

    for index, source in enumerate(sources):
        source_id = source_ids[index] if index < len(source_ids) else source.id
        if source.entity_type not in PROJECTABLE_ENTITY_TYPES:
            skipped += 1
            continue
        projected_source = source.model_copy(update={"id": source_id})
        extracted = extract_projected_memory_entities(
            projected_source,
            max_entities=max_entities,
            min_confidence=min_confidence,
        )
        if not extracted:
            skipped += 1
            continue
        extracted_count += len(extracted)
        _add_projection_candidates(
            group_id=group_id,
            now=now,
            source=projected_source,
            source_id=source_id,
            candidates=extracted,
            projected_by_id=projected_by_id,
            relationships=relationships,
        )

    return await _persist_projection_batch(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources_count=len(sources),
        extracted_count=extracted_count,
        skipped=skipped,
        projected_by_id=projected_by_id,
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
        _add_projection_candidates(
            group_id=group_id,
            now=now,
            source=projected_source,
            source_id=source_id,
            candidates=extracted,
            projected_by_id=projected_by_id,
            relationships=relationships,
        )

    return await _persist_projection_batch(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources_count=len(sources),
        extracted_count=extracted_count,
        skipped=skipped,
        projected_by_id=projected_by_id,
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
    relationships: list[Relationship],
    generate_embeddings: bool,
) -> MemoryProjectionBatchResult:
    if not projected_by_id:
        return MemoryProjectionBatchResult(sources=0, skipped=skipped)

    errors: list[str] = []
    created_ids: list[str]
    try:
        entities_to_create = await _missing_projected_entities(
            entity_manager, list(projected_by_id.values())
        )
        created_ids = await _create_projected_entities(
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
            errors=(str(exc),),
        )

    relationship_count = 0
    if relationships:
        try:
            created, failed = await relationship_manager.create_bulk(relationships)
            relationship_count = created
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
        projected_entities=len(created_ids),
        relationships=relationship_count,
        skipped=skipped,
        errors=tuple(errors),
    )


async def _create_projected_entities(
    entity_manager: Any,
    entities: Sequence[Entity],
    *,
    generate_embeddings: bool,
) -> list[str]:
    if not entities:
        return []

    create_direct_bulk = getattr(entity_manager, "create_direct_bulk", None)
    if callable(create_direct_bulk):
        return list(
            await create_direct_bulk(
                list(entities),
                generate_embeddings=generate_embeddings,
            )
        )

    bulk_create_direct = getattr(entity_manager, "bulk_create_direct", None)
    if callable(bulk_create_direct):
        created, failed = await bulk_create_direct(list(entities))
        if failed:
            raise RuntimeError(f"{failed} projected entities failed to persist")
        if created != len(entities):
            raise RuntimeError(f"projected entity count mismatch: {created}/{len(entities)}")
        return [entity.id for entity in entities]

    create_direct = getattr(entity_manager, "create_direct", None)
    if callable(create_direct):
        return [
            await create_direct(entity, generate_embedding=generate_embeddings)
            for entity in entities
        ]

    return [await entity_manager.create(entity) for entity in entities]


def _add_projection_candidates(
    *,
    group_id: str,
    now: datetime,
    source: Entity,
    source_id: str,
    candidates: Sequence[ProjectedMemoryEntity],
    projected_by_id: dict[str, Entity],
    relationships: list[Relationship],
) -> None:
    for candidate in candidates:
        entity = _projected_entity(
            candidate,
            group_id=group_id,
            now=now,
            source=source,
        )
        projected_by_id.setdefault(entity.id, entity)
        relationships.append(
            _projection_relationship(
                source=source,
                source_id=source_id,
                target_id=entity.id,
                candidate=candidate,
                now=now,
            )
        )


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


def _relationship_fact(source: Entity, candidate: ProjectedMemoryEntity) -> str:
    source_name = source.name or source.id
    if candidate.context:
        return f"{source_name} mentions {candidate.name}: {candidate.context}"
    return f"{source_name} mentions {candidate.name}"


def _source_text(source: Entity) -> str:
    body = " ".join(part for part in (source.description, source.content) if part)
    return body or source.name


def _inherited_scope_metadata(source: Entity) -> dict[str, object]:
    metadata = dict(source.metadata or {})
    return {
        key: metadata[key]
        for key in _SCOPE_METADATA_KEYS
        if key in metadata and metadata[key] is not None
    }


def _projection_identity_scope(source: Entity) -> str:
    metadata = dict(source.metadata or {})
    memory_scope = str(metadata.get("memory_scope") or "org")
    scope_key = str(
        metadata.get("scope_key")
        or metadata.get("project_id")
        or metadata.get("agent_id")
        or "default"
    )
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
