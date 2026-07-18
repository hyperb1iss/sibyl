"""Authoritative operational-source expansion for retrieval."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.projection import MANIFEST_STATE_COMPLETE, operational_experience_manifest_id
from sibyl_core.retrieval.query_ranking import extract_keywords

OperationalSourceStatus = Literal[
    "complete",
    "inventory_incomplete",
    "inventory_invalid",
    "manifest_invalid",
    "manifest_missing",
    "manifest_not_complete",
    "project_denied",
    "scope_denied",
]


class OperationalSourceEntityReader(Protocol):
    async def get(self, entity_id: str) -> Entity | None: ...

    async def get_many(self, entity_ids: Sequence[str]) -> list[Entity]: ...


@dataclass(frozen=True, slots=True)
class OperationalSourceInventory:
    source_id: str
    manifest_id: str
    status: OperationalSourceStatus
    raw_observations: tuple[Entity, ...] = ()
    expected_entity_count: int = 0
    loaded_entity_count: int = 0
    memory_scope: str | None = None
    project_id: str | None = None
    scope_key: str | None = None


@dataclass(frozen=True, slots=True)
class OperationalSourceSpan:
    source_id: str
    entities: tuple[Entity, ...]
    observation_ordinals: tuple[int, ...]
    candidate_window_count: int
    ranking_applied: bool


async def fetch_operational_source_inventory(
    entity_reader: OperationalSourceEntityReader,
    source_id: str,
    *,
    allowed_project_ids: set[str] | None = None,
    allowed_memory_scope_keys: set[str] | None = None,
    principal_id: str | None = None,
) -> OperationalSourceInventory:
    """Fetch and validate one complete manifest-owned operational source."""
    normalized_source_id = source_id.strip()
    if not normalized_source_id:
        raise ValueError("source_id must not be empty")

    manifest_id = operational_experience_manifest_id(normalized_source_id)
    try:
        manifest = await entity_reader.get(manifest_id)
    except KeyError:
        return _inventory(normalized_source_id, manifest_id, "manifest_missing")
    if manifest is None:
        return _inventory(normalized_source_id, manifest_id, "manifest_missing")

    metadata = manifest.metadata
    memory_scope = _metadata_string(metadata, "memory_scope")
    project_id = _metadata_string(metadata, "project_id")
    scope_key = _metadata_string(metadata, "scope_key")
    owner_principal_id = _metadata_string(metadata, "principal_id")
    expected_ids = metadata.get("expected_entity_ids")
    if (
        manifest.entity_type is not EntityType.ARTIFACT
        or metadata.get("projection_kind") != "manifest"
        or metadata.get("operational_source_id") != normalized_source_id
        or not isinstance(expected_ids, list)
        or not expected_ids
        or any(not isinstance(entity_id, str) or not entity_id for entity_id in expected_ids)
        or len(expected_ids) != len(set(expected_ids))
        or manifest_id not in expected_ids
    ):
        return _inventory(
            normalized_source_id,
            manifest_id,
            "manifest_invalid",
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )
    if metadata.get("operational_projection_state") != MANIFEST_STATE_COMPLETE:
        return _inventory(
            normalized_source_id,
            manifest_id,
            "manifest_not_complete",
            expected_entity_count=len(expected_ids),
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )
    if allowed_project_ids is not None and project_id not in allowed_project_ids:
        return _inventory(
            normalized_source_id,
            manifest_id,
            "project_denied",
            expected_entity_count=len(expected_ids),
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )
    scope_denied = False
    if memory_scope == "private":
        conflicting_owner = bool(
            scope_key and owner_principal_id and scope_key != owner_principal_id
        )
        owner = owner_principal_id or scope_key
        if conflicting_owner or not owner:
            scope_denied = True
        elif allowed_memory_scope_keys is not None:
            scope_denied = (
                memory_scope_policy_key(memory_scope, owner) not in allowed_memory_scope_keys
            )
        else:
            scope_denied = not principal_id or owner != principal_id
    elif memory_scope is not None and allowed_memory_scope_keys is not None:
        scope_denied = (
            memory_scope_policy_key(memory_scope, scope_key) not in allowed_memory_scope_keys
        )
    if scope_denied:
        return _inventory(
            normalized_source_id,
            manifest_id,
            "scope_denied",
            expected_entity_count=len(expected_ids),
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )

    entities = await entity_reader.get_many(expected_ids)
    if {entity.id for entity in entities} != set(expected_ids):
        return _inventory(
            normalized_source_id,
            manifest_id,
            "inventory_incomplete",
            expected_entity_count=len(expected_ids),
            loaded_entity_count=len(entities),
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )
    loaded_manifest = next(entity for entity in entities if entity.id == manifest_id)
    if loaded_manifest.metadata.get("operational_projection_state") != MANIFEST_STATE_COMPLETE:
        return _inventory(
            normalized_source_id,
            manifest_id,
            "manifest_not_complete",
            expected_entity_count=len(expected_ids),
            loaded_entity_count=len(entities),
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )
    if loaded_manifest.metadata.get("expected_entity_ids") != expected_ids:
        return _inventory(
            normalized_source_id,
            manifest_id,
            "inventory_incomplete",
            expected_entity_count=len(expected_ids),
            loaded_entity_count=len(entities),
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )
    if _metadata_string(loaded_manifest.metadata, "principal_id") != owner_principal_id:
        return _inventory(
            normalized_source_id,
            manifest_id,
            "inventory_incomplete",
            expected_entity_count=len(expected_ids),
            loaded_entity_count=len(entities),
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )
    if any(
        entity.metadata.get("operational_source_id") != normalized_source_id
        or _metadata_string(entity.metadata, "memory_scope") != memory_scope
        or _metadata_string(entity.metadata, "project_id") != project_id
        or _metadata_string(entity.metadata, "scope_key") != scope_key
        or _metadata_string(entity.metadata, "principal_id") != owner_principal_id
        for entity in entities
    ):
        return _inventory(
            normalized_source_id,
            manifest_id,
            "inventory_invalid",
            expected_entity_count=len(expected_ids),
            loaded_entity_count=len(entities),
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )

    raw_observations = [
        entity for entity in entities if entity.metadata.get("projection_kind") == "raw_observation"
    ]
    if any(_observation_position(entity) is None for entity in raw_observations):
        return _inventory(
            normalized_source_id,
            manifest_id,
            "inventory_invalid",
            expected_entity_count=len(expected_ids),
            loaded_entity_count=len(entities),
            memory_scope=memory_scope,
            project_id=project_id,
            scope_key=scope_key,
        )
    raw_observations.sort(key=lambda entity: (*_required_observation_position(entity), entity.id))
    return _inventory(
        normalized_source_id,
        manifest_id,
        "complete",
        raw_observations=tuple(raw_observations),
        expected_entity_count=len(expected_ids),
        loaded_entity_count=len(entities),
        memory_scope=memory_scope,
        project_id=project_id,
        scope_key=scope_key,
    )


def select_operational_source_span(
    query: str,
    inventory: OperationalSourceInventory,
    *,
    max_observations: int = 4,
    max_entities: int = 6,
) -> OperationalSourceSpan:
    """Select the strongest contiguous observation window and preserve source order."""
    if max_observations < 1 or max_entities < 1:
        raise ValueError("span limits must be positive")
    if inventory.status != "complete" or not inventory.raw_observations:
        return OperationalSourceSpan(
            source_id=inventory.source_id,
            entities=(),
            observation_ordinals=(),
            candidate_window_count=0,
            ranking_applied=False,
        )

    grouped: dict[int, list[Entity]] = defaultdict(list)
    for entity in inventory.raw_observations:
        position = _required_observation_position(entity)
        grouped[position[0]].append(entity)
    observation_groups = [tuple(grouped[ordinal]) for ordinal in sorted(grouped)]
    entity_terms, group_terms = _source_discriminative_terms(observation_groups)
    query_terms = frozenset(extract_keywords(query))
    query_term_weights = _query_term_weights(query_terms, group_terms)
    window_size = min(max_observations, len(observation_groups))
    candidate_window_count = len(observation_groups) - window_size + 1
    selected_start = max(
        range(candidate_window_count),
        key=lambda start: (
            *_window_coverage_score(
                query_terms,
                group_terms[start : start + window_size],
                query_term_weights,
            ),
            -_required_observation_position(observation_groups[start][0])[0],
        ),
    )
    selected_window = tuple(observation_groups[selected_start : selected_start + window_size])
    selected_entities = _select_window_entities(
        selected_window,
        max_entities=max_entities,
        entity_terms=entity_terms,
        query_terms=query_terms,
        query_term_weights=query_term_weights,
    )
    return OperationalSourceSpan(
        source_id=inventory.source_id,
        entities=tuple(selected_entities),
        observation_ordinals=tuple(
            dict.fromkeys(_required_observation_position(entity)[0] for entity in selected_entities)
        ),
        candidate_window_count=candidate_window_count,
        ranking_applied=candidate_window_count > 1 and selected_start != 0,
    )


def operational_observation_signal_text(entity: Entity) -> str:
    """Return state-local evidence without source-wide headers repeated on every observation."""
    return "\n".join(_operational_observation_signal_lines(entity))


def _operational_observation_signal_lines(entity: Entity) -> list[str]:
    lines: list[str] = []
    in_evidence = False
    for raw_line in (entity.content or entity.description).splitlines():
        line = raw_line.strip()
        if line == "Evidence:":
            in_evidence = True
            continue
        if in_evidence:
            if line:
                lines.append(line)
            continue
        label, separator, value = line.partition(":")
        if separator and label.casefold() in {
            "action producing this observation",
            "reasoning",
            "uri",
        }:
            lines.append(value.strip())
        elif line == "Initial observation before any recorded action.":
            lines.append(line)
    return lines


def _source_discriminative_terms(
    observation_groups: list[tuple[Entity, ...]],
) -> tuple[dict[str, frozenset[str]], list[frozenset[str]]]:
    signal_lines = {
        entity.id: tuple(_operational_observation_signal_lines(entity))
        for group in observation_groups
        for entity in group
    }
    common_lines: set[str] = set()
    if len(observation_groups) > 1:
        group_lines = [
            {line.casefold() for entity in group for line in signal_lines[entity.id]}
            for group in observation_groups
        ]
        common_lines = set.intersection(*group_lines)
    entity_terms = {
        entity_id: frozenset(
            extract_keywords(
                "\n".join(line for line in lines if line.casefold() not in common_lines)
            )
        )
        for entity_id, lines in signal_lines.items()
    }
    group_terms = [
        frozenset(term for entity in group for term in entity_terms[entity.id])
        for group in observation_groups
    ]
    return entity_terms, group_terms


def _query_term_weights(
    query_terms: frozenset[str],
    group_terms: list[frozenset[str]],
) -> dict[str, int]:
    weights = {
        term: len(group_terms) - sum(term in terms for terms in group_terms) for term in query_terms
    }
    if any(weights.values()):
        return weights
    return {term: 1 for term in query_terms}


def _select_window_entities(
    window: tuple[tuple[Entity, ...], ...],
    *,
    max_entities: int,
    entity_terms: dict[str, frozenset[str]],
    query_terms: frozenset[str],
    query_term_weights: dict[str, int],
) -> list[Entity]:
    representatives = [
        max(
            group,
            key=lambda entity: (
                _weighted_term_match(entity_terms[entity.id], query_terms, query_term_weights),
                tuple(-value for value in _required_observation_position(entity)),
                entity.id,
            ),
        )
        for group in window
    ]
    if len(representatives) > max_entities:
        representatives = sorted(
            representatives,
            key=lambda entity: (
                -_weighted_term_match(entity_terms[entity.id], query_terms, query_term_weights),
                *_required_observation_position(entity),
                entity.id,
            ),
        )[:max_entities]
    selected_ids = {entity.id for entity in representatives}
    flattened = [entity for group in window for entity in group]
    for entity in flattened:
        if len(selected_ids) >= max_entities:
            break
        selected_ids.add(entity.id)
    return [entity for entity in flattened if entity.id in selected_ids]


def _window_coverage_score(
    query_terms: frozenset[str],
    group_terms: list[frozenset[str]],
    query_term_weights: dict[str, int],
) -> tuple[float, int, int]:
    if not query_terms:
        return 0.0, 0, 0
    matched_terms = set().union(*group_terms) & query_terms
    total_weight = sum(query_term_weights.values())
    return (
        sum(query_term_weights[term] for term in matched_terms) / total_weight,
        sum(
            _weighted_term_match(terms, query_terms, query_term_weights) > 0
            for terms in group_terms
        ),
        sum(_weighted_term_match(terms, query_terms, query_term_weights) for terms in group_terms),
    )


def _weighted_term_match(
    terms: frozenset[str],
    query_terms: frozenset[str],
    query_term_weights: dict[str, int],
) -> int:
    return sum(query_term_weights[term] for term in terms & query_terms)


def _observation_position(entity: Entity) -> tuple[int, int] | None:
    ordinal = entity.metadata.get("observation_ordinal")
    part_index = entity.metadata.get("evidence_part_index")
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or isinstance(part_index, bool)
        or not isinstance(part_index, int)
        or ordinal < 0
        or part_index < 0
    ):
        return None
    return ordinal, part_index


def _required_observation_position(entity: Entity) -> tuple[int, int]:
    position = _observation_position(entity)
    if position is None:
        raise ValueError(f"operational observation {entity.id} has invalid ordering metadata")
    return position


def _metadata_string(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _inventory(
    source_id: str,
    manifest_id: str,
    status: OperationalSourceStatus,
    *,
    raw_observations: tuple[Entity, ...] = (),
    expected_entity_count: int = 0,
    loaded_entity_count: int = 0,
    memory_scope: str | None = None,
    project_id: str | None = None,
    scope_key: str | None = None,
) -> OperationalSourceInventory:
    return OperationalSourceInventory(
        source_id=source_id,
        manifest_id=manifest_id,
        status=status,
        raw_observations=raw_observations,
        expected_entity_count=expected_entity_count,
        loaded_entity_count=loaded_entity_count,
        memory_scope=memory_scope,
        project_id=project_id,
        scope_key=scope_key,
    )


__all__ = [
    "OperationalSourceEntityReader",
    "OperationalSourceInventory",
    "OperationalSourceSpan",
    "OperationalSourceStatus",
    "fetch_operational_source_inventory",
    "operational_observation_signal_text",
    "select_operational_source_span",
]
