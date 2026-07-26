"""Authoritative operational-source expansion for retrieval."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    private_scope_granted_for,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.projection import MANIFEST_STATE_COMPLETE, operational_experience_manifest_id
from sibyl_core.retrieval.query_ranking import extract_keywords

PASSAGE_PROJECTION_KIND = "passage"
RAW_OBSERVATION_PROJECTION_KIND = "raw_observation"
_UNIT_PROJECTION_KINDS = frozenset({PASSAGE_PROJECTION_KIND, RAW_OBSERVATION_PROJECTION_KIND})

# A passage is roughly a twelfth of the state it was cut from, so a single one
# carries the gold less often than the whole state did. Three adjacent passages
# close that gap exactly: the offline oracle scored 95.5% enterprise / 100% web
# exposure for a 3-adjacent window against 95.5% / 100% for the whole state,
# where a lone passage reached only 93.2% / 97.9%.
PASSAGE_WINDOW_UNITS = 3

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
    """One source's ordered retrieval units.

    `raw_observations` holds the finest unit each evidence part offers: the
    passages cut from it when it was sliced, and the whole-part row when it was
    not. `passage_count` says how many of those units are passages.
    """

    source_id: str
    manifest_id: str
    status: OperationalSourceStatus
    raw_observations: tuple[Entity, ...] = ()
    expected_entity_count: int = 0
    loaded_entity_count: int = 0
    memory_scope: str | None = None
    project_id: str | None = None
    scope_key: str | None = None
    passage_count: int = 0


@dataclass(frozen=True, slots=True)
class OperationalSourceSpan:
    source_id: str
    entities: tuple[Entity, ...]
    observation_ordinals: tuple[int, ...]
    candidate_window_count: int
    ranking_applied: bool
    passage_count: int = 0


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
    # This surface returns raw observation content, so the audience question is
    # the shared one. The band it used to answer itself only denied when an
    # API-key grant was present, which left team, delegated, organization,
    # shared and public manifests visible to any session without one.
    scope_denied = not memory_metadata_read_allowed(
        {
            "memory_scope": memory_scope,
            "principal_id": owner_principal_id,
            "scope_key": scope_key,
        },
        principal_id=principal_id,
        accessible_projects=allowed_project_ids,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        private_scope_granted=private_scope_granted_for(
            allowed_memory_scope_keys, principal_id=principal_id
        ),
        # The project gate for a manifest is the allowed_project_ids check
        # above, which treats an unknown set as "no project filtering" for the
        # operator paths that read inventories. Re-deriving it here would apply
        # the stricter read semantics and deny those.
        row_project_id=None,
    )
    if memory_scope == "private" and scope_key and owner_principal_id != scope_key:
        # Two owner channels naming different principals is a shape no write
        # produces; refuse rather than picking one.
        scope_denied = scope_denied or bool(owner_principal_id)
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

    projected = [
        entity
        for entity in entities
        if entity.metadata.get("projection_kind") in _UNIT_PROJECTION_KINDS
    ]
    if any(_observation_position(entity) is None for entity in projected):
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
    units = _finest_granularity_units(projected)
    units.sort(key=lambda entity: (*_required_observation_position(entity), entity.id))
    return _inventory(
        normalized_source_id,
        manifest_id,
        "complete",
        raw_observations=tuple(units),
        expected_entity_count=len(expected_ids),
        loaded_entity_count=len(entities),
        memory_scope=memory_scope,
        project_id=project_id,
        scope_key=scope_key,
        passage_count=sum(_is_passage(entity) for entity in units),
    )


def select_operational_source_span(
    query: str,
    inventory: OperationalSourceInventory,
    *,
    max_observations: int = 4,
    max_entities: int = 6,
) -> OperationalSourceSpan:
    """Select the best-scoring contiguous window of units and preserve source order.

    The guarantee here is *best-scoring*, not *containing the answer*: the
    window is picked by query-term coverage over the source's own
    discriminative vocabulary. The offline oracle that measured a 3-adjacent
    passage window at the whole-state exposure ceiling measured window
    **existence** — that some window carries the gold — which no ranked
    selection inherits.
    """
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

    observation_groups = _unit_groups(inventory.raw_observations)
    entity_terms, group_terms = _source_discriminative_terms(observation_groups)
    query_terms = frozenset(extract_keywords(query))
    query_term_weights = _query_term_weights(query_terms, group_terms)
    candidate_windows = _candidate_windows(
        observation_groups,
        max_observations=max_observations,
    )
    selected_start, selected_size = max(
        candidate_windows,
        key=lambda window: (
            *_window_coverage_score(
                query_terms,
                group_terms[window[0] : window[0] + window[1]],
                query_term_weights,
            ),
            -_required_observation_position(observation_groups[window[0]][0])[0],
        ),
    )
    selected_window = tuple(observation_groups[selected_start : selected_start + selected_size])
    # A partial window is the regression the window exists to avoid, so a
    # caller's item allowance may shrink the pack but never cut the window
    # itself below the adjacency that was measured.
    selected_is_sliced = any(_is_passage(entity) for group in selected_window for entity in group)
    entity_budget = max(max_entities, selected_size) if selected_is_sliced else max_entities
    selected_entities = _select_window_entities(
        selected_window,
        max_entities=entity_budget,
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
        candidate_window_count=len(candidate_windows),
        ranking_applied=(
            len(candidate_windows) > 1 and (selected_start, selected_size) != candidate_windows[0]
        ),
        passage_count=sum(_is_passage(entity) for entity in selected_entities),
    )


def operational_observation_signal_text(entity: Entity) -> str:
    """Return state-local evidence without source-wide headers repeated on every observation."""
    return "\n".join(_operational_observation_signal_lines(entity))


def _is_passage(entity: Entity) -> bool:
    return entity.metadata.get("projection_kind") == PASSAGE_PROJECTION_KIND


def _finest_granularity_units(entities: list[Entity]) -> list[Entity]:
    """Keep the finest unit each evidence part offers.

    A passage and the evidence part it was cut from carry the same text, so
    admitting both would make one window pay twice for one state. Parts that
    were never sliced have only their whole-part row to offer and keep it.
    """
    sliced_parts = {
        _required_observation_position(entity)[:2] for entity in entities if _is_passage(entity)
    }
    return [
        entity
        for entity in entities
        if _is_passage(entity) or _required_observation_position(entity)[:2] not in sliced_parts
    ]


def _group_key(entity: Entity) -> tuple[int, ...]:
    position = _required_observation_position(entity)
    return position if _is_passage(entity) else position[:1]


def _unit_groups(units: Sequence[Entity]) -> list[tuple[Entity, ...]]:
    """Group ordered units into the stops a window slides over.

    Every evidence part of one unsliced observation shares a stop, which is the
    whole-state granularity that shape has always been windowed at. A passage is
    its own stop, so a window of three groups is three adjacent passages.
    """
    groups: list[list[Entity]] = []
    previous_key: tuple[int, ...] | None = None
    for entity in units:
        key = _group_key(entity)
        if key != previous_key:
            groups.append([])
            previous_key = key
        groups[-1].append(entity)
    return [tuple(group) for group in groups]


def _run_key(entity: Entity) -> tuple[int, int] | None:
    """Say which stretch of groups one window may slide within.

    Passages key on the body they were cut from, so a window cannot cross into
    another: three passages spanning two states are contiguous in source order
    but are not the adjacency the exposure measurement is about. Whole-part rows
    all key alike, because sliding across observations is the adjacency that
    shape has always had, and a source that slices one part must not narrow the
    windows of the parts it left whole.
    """
    return _required_observation_position(entity)[:2] if _is_passage(entity) else None


def _candidate_windows(
    groups: Sequence[tuple[Entity, ...]],
    *,
    max_observations: int,
) -> list[tuple[int, int]]:
    """Enumerate admissible (start, size) windows over the group list.

    Each run is sized by what it holds: passages by the measured adjacency,
    whole-part rows by what the caller asked for. A run shorter than its own
    window yields one short window rather than none, which is what keeps a
    two-passage body and a lone unsliced part reachable at all.
    """
    windows: list[tuple[int, int]] = []
    for run_start, run_stop in _window_runs(groups):
        window_size = (
            PASSAGE_WINDOW_UNITS if _is_passage(groups[run_start][0]) else max_observations
        )
        size = min(window_size, run_stop - run_start)
        windows.extend((start, size) for start in range(run_start, run_stop - size + 1))
    return windows


def _window_runs(groups: Sequence[tuple[Entity, ...]]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(groups) + 1):
        if index == len(groups) or _run_key(groups[index][0]) != _run_key(groups[start][0]):
            runs.append((start, index))
            start = index
    return runs


def _passage_signal_lines(entity: Entity) -> list[str]:
    """Treat a passage's whole body as signal, minus its locator line.

    A passage is already free of the source-wide preamble the whole-part rows
    repeat, so nothing needs stripping from the body. The locator the renderer
    prepends does need dropping: it restates the observation and the passage's
    own position, which every passage of a body repeats with a different
    number, so the shared-line filter would let it through as discriminative
    vocabulary it is not. The URI comes from metadata instead, matching what
    the whole-part shape contributes.
    """
    uri = entity.metadata.get("uri")
    lines = [uri] if isinstance(uri, str) and uri else []
    lines.extend(line.strip() for line in (entity.content or "").splitlines()[1:])
    return [line for line in lines if line]


def _operational_observation_signal_lines(entity: Entity) -> list[str]:
    if _is_passage(entity):
        return _passage_signal_lines(entity)
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


def _ordering_index(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _observation_position(entity: Entity) -> tuple[int, int, int] | None:
    """Order a unit by observation, then evidence part, then position within it.

    A whole evidence part and the passages cut from it never coexist as units
    for the same part, so an unsliced part takes passage slot 0 without ever
    colliding with a passage.
    """
    ordinal = _ordering_index(entity.metadata.get("observation_ordinal"))
    part_index = _ordering_index(entity.metadata.get("evidence_part_index"))
    if ordinal is None or part_index is None:
        return None
    if not _is_passage(entity):
        return ordinal, part_index, 0
    passage_index = _ordering_index(entity.metadata.get("passage_index"))
    if passage_index is None:
        return None
    return ordinal, part_index, passage_index


def _required_observation_position(entity: Entity) -> tuple[int, int, int]:
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
    passage_count: int = 0,
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
        passage_count=passage_count,
    )


__all__ = [
    "PASSAGE_PROJECTION_KIND",
    "PASSAGE_WINDOW_UNITS",
    "RAW_OBSERVATION_PROJECTION_KIND",
    "OperationalSourceEntityReader",
    "OperationalSourceInventory",
    "OperationalSourceSpan",
    "OperationalSourceStatus",
    "fetch_operational_source_inventory",
    "operational_observation_signal_text",
    "select_operational_source_span",
]
