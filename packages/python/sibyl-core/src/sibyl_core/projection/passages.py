"""Mint passage entities beside a memory whose body is too big to serve whole.

A1 built the slice substrate against benchmark states, but the product's own
memories have the same shape: measured over the durable memories a real export
pulled, two thirds exceed ``HARD_MAX`` and are stored and served as one blob.
This is the write-side half that puts them in the band.

Passages are additive. The parent keeps its full body and stays the thing a
citation resolves to; each passage is an independently retrievable span that
points back with PART_OF. Nothing is deleted and nothing is truncated, so a
reader that finds a passage can always widen to the memory it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from sibyl_core.memory_pipeline.spans import (
    MAX_PASSAGE_CONTENT_CHARS,
    MAX_PASSAGES_PER_SOURCE,
    AgentSpan,
    MemoryStructureError,
    agent_atomic_from_metadata,
    agent_spans_from_metadata,
    validate_agent_spans,
)
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.projection.slicing import HARD_MAX, Slice, render_slice, slice_prose
from sibyl_core.tools.helpers import _generate_id

if TYPE_CHECKING:
    from collections.abc import Sequence

log = structlog.get_logger()

PASSAGE_PROJECTION_KIND = "passage"

# Which cutter produced a span, stamped on every passage. The replay job and any
# audit of retrievability need to tell an agent's own seams apart from the ones
# the prose cutter inferred, and a passage read alone carries no other clue.
PASSAGE_PLAN_KEY = "passage_plan"
PASSAGE_PLAN_AGENT = "agent"
PASSAGE_PLAN_MECHANICAL = "mechanical"

# Set only when the spans between them account for the whole parent body.
# Retrieval reads it to decide whether the spans may stand in for the memory.
PASSAGE_COVERS_PARENT_KEY = "passage_covers_parent"

# Only bodies past the packer's own ceiling are worth cutting. Below it the
# body already fits one slice, so passages would duplicate the parent and pay
# an embedding for the privilege.
PASSAGE_MIN_SOURCE_CHARS = HARD_MAX

# Prose-bearing memory kinds. Work items are excluded on purpose: their content
# mirrors a short description rather than carrying a body of its own. Passages
# are excluded so a re-projection can never slice a slice.
PASSAGE_SOURCE_TYPES = frozenset(
    {
        EntityType.ARTIFACT,
        EntityType.CLAIM,
        EntityType.DECISION,
        EntityType.DOCUMENT,
        EntityType.DOMAIN,
        EntityType.EPISODE,
        EntityType.ERROR_PATTERN,
        EntityType.EVENT,
        EntityType.GUIDE,
        EntityType.IDEA,
        EntityType.NOTE,
        EntityType.PATTERN,
        EntityType.PLAN,
        EntityType.PREFERENCE,
        EntityType.PROCEDURE,
        EntityType.RULE,
        EntityType.SESSION,
        EntityType.TEMPLATE,
    }
)

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
class PassageProjectionResult:
    """What one source's passage projection produced."""

    source_id: str
    passages: int = 0
    relationships: int = 0
    skipped: bool = False
    reason: str | None = None
    created_passages: tuple[Entity, ...] = field(default_factory=tuple)
    created_relationships: tuple[Relationship, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


def passage_entity_id(source_id: str, passage_index: int) -> str:
    """Address a passage by its parent and position, so re-projection is stable."""
    return _generate_id("passage", "memory", source_id, str(passage_index))


def should_project_passages(source: Entity) -> bool:
    """Whether this memory is the kind, and the size, worth cutting.

    An agent's own declarations outrank the size heuristic in both directions.
    ``agent_atomic`` means the body is one retrievable unit and must not be cut
    at any length. A supplied span plan means the writer named seams it wants
    served, so the plan is honored even under the threshold the mechanical
    cutter waits for: the heuristic exists because the cutter is guessing, and
    here it is not.
    """
    if source.entity_type not in PASSAGE_SOURCE_TYPES:
        return False
    metadata = source.metadata or {}
    if agent_atomic_from_metadata(metadata):
        return False
    if agent_spans_from_metadata(metadata):
        return True
    return len(source.content or "") > PASSAGE_MIN_SOURCE_CHARS


def plan_entity_passages(
    source: Entity,
    *,
    source_id: str,
    group_id: str,
    now: datetime | None = None,
) -> tuple[list[Entity], list[Relationship]]:
    """Cut one memory into passage entities and the edges back to it.

    Pure: builds the rows without touching a manager, so the boundary rules and
    the metadata contract are testable without a database.
    """
    if not should_project_passages(source):
        return [], []
    if source.organization_id and str(source.organization_id) != str(group_id):
        # Cheap guard at a tenant boundary. Both callers pass the org the parent
        # was just written to, and namespace-per-org means a foreign row should
        # not be fetchable here in the first place, so this is unreachable today
        # and stays that way loudly rather than by assumption.
        msg = (
            f"refusing to project passages for {source.id}: parent belongs to "
            f"{source.organization_id}, not {group_id}"
        )
        raise ValueError(msg)

    # Not named ``content``: the loop below rebinds that name to each rendered
    # passage, and the parent body has to survive the whole walk.
    body = source.content or ""
    slices, plan_kind = _plan_slices(body, source)
    if len(slices) < 2:
        # One slice is the parent again. The projection has to earn its row.
        return [], []

    stamped = now or datetime.now(UTC)
    scope = _inherited_scope_metadata(source)
    total = min(len(slices), MAX_PASSAGES_PER_SOURCE)
    # A projection that drops spans must not let its survivors stand in for the
    # whole memory, or the dropped text becomes unreachable at read time.
    covers_parent = total == len(slices)
    entities: list[Entity] = []
    relationships: list[Relationship] = []

    for passage_index, passage in enumerate(slices[:total]):
        header = _passage_header(source, passage_index + 1, total)
        content = render_slice(header, passage.breadcrumb, passage.content)
        if len(content) > MAX_PASSAGE_CONTENT_CHARS:
            # The trail restates ancestors the parent row still holds; the
            # span's own bytes are the only copy, so trail characters go first.
            budget = MAX_PASSAGE_CONTENT_CHARS - len(header) - len(passage.content) - 2
            trail = passage.breadcrumb[-budget:] if budget > 0 else ""
            content = render_slice(header, trail, passage.content)
        if len(content) > MAX_PASSAGE_CONTENT_CHARS:
            # A single unbroken line past the ceiling. Cutting mid-line would
            # bisect a literal, so the span stays reachable through its parent
            # only, which means these passages no longer cover it.
            covers_parent = False
            continue

        entity_id = passage_entity_id(source_id, passage_index)
        entities.append(
            Entity(
                id=entity_id,
                entity_type=EntityType.PASSAGE,
                name=f"{source.name} · passage {passage_index + 1}/{total}",
                # Deliberately the span itself rather than a shared blurb: one
                # description reused across every passage makes them
                # indistinguishable to anything reading descriptions, which is
                # the defect 07921a5e fixed on the read side.
                description=_summary(passage.content),
                content=content,
                organization_id=group_id,
                created_by=source.created_by,
                modified_by=source.created_by,
                metadata={
                    **scope,
                    "category": "passage_projection",
                    "tags": ["projected", PASSAGE_PROJECTION_KIND],
                    "organization_id": group_id,
                    "projection_kind": PASSAGE_PROJECTION_KIND,
                    "parent_entity_id": source_id,
                    "source_entity_id": source_id,
                    "source_entity_type": source.entity_type.value,
                    "passage_index": passage_index,
                    "passage_total": total,
                    "passage_breadcrumb": passage.breadcrumb,
                    "passage_cut_reason": passage.reason,
                    PASSAGE_PLAN_KEY: plan_kind,
                    PASSAGE_COVERS_PARENT_KEY: True,
                },
                created_at=stamped,
                updated_at=stamped,
            )
        )
        relationships.append(
            Relationship(
                id=_generate_id("rel", entity_id, RelationshipType.PART_OF.value, source_id),
                source_id=entity_id,
                target_id=source_id,
                relationship_type=RelationshipType.PART_OF,
                weight=1.0,
                metadata={
                    "created_at": stamped.isoformat(),
                    "auto_projected": True,
                    "projection_kind": PASSAGE_PROJECTION_KIND,
                    "passage_index": passage_index,
                    "passage_total": total,
                    **scope,
                },
                created_at=stamped,
            )
        )

    if not covers_parent:
        # A skip can happen on the last slice, so coverage is only known once
        # the whole body has been walked. Stamp it on every span rather than
        # letting the early ones claim a completeness they do not have.
        entities = [
            entity.model_copy(
                update={"metadata": {**entity.metadata, PASSAGE_COVERS_PARENT_KEY: False}}
            )
            for entity in entities
        ]

    return entities, relationships


async def project_entity_passages(
    *,
    entity_manager: Any,
    relationship_manager: Any,
    source: Entity,
    group_id: str,
    created_source_id: str | None = None,
    generate_embeddings: bool = True,
) -> PassageProjectionResult:
    """Persist the passages for one memory that has already been written.

    The parent must exist before this runs. Writing an edge to a row that is
    not there yet is how the operational-experience projection silently dropped
    every PART_OF it created and never self-healed, so the ordering here is
    load-bearing: entities first, then the edges that point at them.
    """
    source_id = created_source_id or source.id
    entities, relationships = plan_entity_passages(
        source,
        source_id=source_id,
        group_id=group_id,
    )
    if not entities:
        return PassageProjectionResult(
            source_id=source_id,
            skipped=True,
            reason="below_threshold" if not should_project_passages(source) else "single_slice",
        )

    errors: list[str] = []
    try:
        created_passages = await _create_passages(
            entity_manager,
            entities,
            generate_embeddings=generate_embeddings,
        )
    except Exception as exc:
        # A memory that stored fine must not fail because its passages did.
        # The parent still holds the whole body, so the loss is retrievability,
        # not content.
        log.warning(
            "passage_projection_entities_failed",
            source_id=source_id,
            passages=len(entities),
            error_type=type(exc).__name__,
        )
        return PassageProjectionResult(
            source_id=source_id,
            reason="entity_write_failed",
            errors=(str(exc),),
        )

    created_ids = {entity.id for entity in created_passages}
    pending = [
        relationship for relationship in relationships if relationship.source_id in created_ids
    ]
    created_relationships: tuple[Relationship, ...] = ()
    if pending:
        try:
            created_relationships = await _create_relationships(
                relationship_manager,
                pending,
                generate_embeddings=generate_embeddings,
            )
            if len(created_relationships) != len(pending):
                errors.append(f"{len(pending) - len(created_relationships)} passage edges failed")
        except Exception as exc:
            log.warning(
                "passage_projection_relationships_failed",
                source_id=source_id,
                passages=len(created_passages),
                error_type=type(exc).__name__,
            )
            errors.append(str(exc))

    return PassageProjectionResult(
        source_id=source_id,
        passages=len(created_passages),
        relationships=len(created_relationships),
        created_passages=created_passages,
        created_relationships=created_relationships,
        errors=tuple(errors),
    )


async def reproject_entity_passages(
    *,
    entity_manager: Any,
    relationship_manager: Any,
    source: Entity,
    group_id: str,
    created_source_id: str | None = None,
    generate_embeddings: bool = True,
) -> PassageProjectionResult:
    """Re-cut a memory whose body changed, retiring spans the new cut does not use.

    Passage ids are deterministic per index, so a shorter body rewrites the
    leading spans in place and would strand the rest under their old ids. Those
    strays are not merely orphans: they hold the previous revision's text and
    would keep being served as if they were current. Minting first and retiring
    after means a reader mid-update sees a stale span rather than a gap.
    """
    source_id = created_source_id or source.id
    result = await project_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=source,
        group_id=group_id,
        created_source_id=source_id,
        generate_embeddings=generate_embeddings,
    )
    retired = await _retire_passages_from(
        entity_manager,
        source_id=source_id,
        first_stale_index=result.passages,
    )
    if retired:
        log.info(
            "passage_projection_retired_stale",
            source_id=source_id,
            retired=retired,
            kept=result.passages,
        )
    return result


async def retire_entity_passages(
    *,
    entity_manager: Any,
    source_id: str,
) -> int:
    """Delete every span cut from one memory.

    For use when the memory itself is gone. A span left behind keeps serving
    the text of something the caller deleted, which is the one outcome a delete
    must not produce.
    """
    return await _retire_passages_from(
        entity_manager,
        source_id=source_id,
        first_stale_index=0,
    )


async def _retire_passages_from(
    entity_manager: Any,
    *,
    source_id: str,
    first_stale_index: int,
) -> int:
    """Delete passages at or past an index, stopping at the first absence.

    The projection always writes a contiguous run from zero, so the first index
    with no row marks the end of any previous run.
    """
    delete = getattr(entity_manager, "delete", None)
    if not callable(delete):
        return 0
    retired = 0
    for index in range(first_stale_index, MAX_PASSAGES_PER_SOURCE):
        try:
            removed = await delete(passage_entity_id(source_id, index))
        except Exception:
            break
        if not removed:
            break
        retired += 1
    return retired


async def _create_passages(
    entity_manager: Any,
    entities: Sequence[Entity],
    *,
    generate_embeddings: bool,
) -> tuple[Entity, ...]:
    create_direct_bulk = getattr(entity_manager, "create_direct_bulk", None)
    if callable(create_direct_bulk):
        created_ids = list(
            await create_direct_bulk(list(entities), generate_embeddings=generate_embeddings)
        )
        return tuple(
            entity.model_copy(update={"id": created_id})
            for entity, created_id in zip(entities, created_ids, strict=False)
        )

    create_direct = getattr(entity_manager, "create_direct", None)
    if callable(create_direct):
        created: list[Entity] = []
        for entity in entities:
            created_id = await create_direct(entity, generate_embedding=generate_embeddings)
            created.append(entity.model_copy(update={"id": created_id}))
        return tuple(created)

    created = []
    for entity in entities:
        created_id = await entity_manager.create(entity)
        created.append(entity.model_copy(update={"id": created_id}))
    return tuple(created)


async def _create_relationships(
    relationship_manager: Any,
    relationships: Sequence[Relationship],
    *,
    generate_embeddings: bool,
) -> tuple[Relationship, ...]:
    create_direct_bulk = getattr(relationship_manager, "create_direct_bulk", None)
    if callable(create_direct_bulk):
        created_ids = set(
            await create_direct_bulk(
                list(relationships),
                generate_embeddings=generate_embeddings,
            )
        )
        return tuple(
            relationship for relationship in relationships if relationship.id in created_ids
        )

    created, _failed = await relationship_manager.create_bulk(list(relationships))
    return tuple(relationships[:created])


def _plan_slices(content: str, source: Entity) -> tuple[list[Slice], str]:
    """Choose the cut plan for one body: the agent's if it has one, else the cutter's.

    A stored plan is re-validated against the body it is about to cut. The write
    path rejects a plan that does not tile its content, so disagreement here means
    the body was rewritten by a path that did not refresh the plan. Refusing to
    project would leave the memory fat with the previous revision's spans still
    beside it, so the mechanical cutter takes over and says so.
    """
    spans = agent_spans_from_metadata(source.metadata)
    if not spans:
        mechanical, _ = slice_prose(content)
        return mechanical, PASSAGE_PLAN_MECHANICAL
    try:
        validate_agent_spans(content, spans)
    except MemoryStructureError as exc:
        log.warning(
            "passage_projection_agent_plan_stale",
            source_id=source.id,
            spans=len(spans),
            content_chars=len(content),
            reason=str(exc),
        )
        mechanical, _ = slice_prose(content)
        return mechanical, PASSAGE_PLAN_MECHANICAL
    return [_slice_from_span(content, span) for span in spans], PASSAGE_PLAN_AGENT


def _slice_from_span(content: str, span: AgentSpan) -> Slice:
    """Render one agent span in the shape the passage builder already consumes.

    The label lands in the breadcrumb slot, which is where the mechanical cutter
    puts the heading trail: it is part of the passage body, so it is indexed for
    lexical search and read by the reader exactly like a slice header.
    """
    return Slice(
        line_indices=[],
        content=span.slice_of(content),
        cut_depth=0,
        breadcrumb=span.label or "",
        reason="agent-span",
    )


def _passage_header(source: Entity, index: int, total: int) -> str:
    """Name the parent on every passage so a span read alone still locates itself."""
    return f"{source.name} · passage {index}/{total}"


def _summary(content: str) -> str:
    flattened = " ".join(content.split())
    return flattened[:360]


def _inherited_scope_metadata(source: Entity) -> dict[str, object]:
    """Carry the parent's scope onto every passage.

    A passage that loses ``memory_scope``/``scope_key`` either leaks past the
    boundary its parent sits behind or vanishes from the packs that filter on
    it. Inheriting verbatim keeps the span exactly as reachable as the memory
    it was cut from, and no more.
    """
    metadata = dict(source.metadata or {})
    return {
        key: metadata[key]
        for key in _SCOPE_METADATA_KEYS
        if key in metadata and metadata[key] is not None
    }


__all__ = [
    "MAX_PASSAGES_PER_SOURCE",
    "MAX_PASSAGE_CONTENT_CHARS",
    "PASSAGE_COVERS_PARENT_KEY",
    "PASSAGE_MIN_SOURCE_CHARS",
    "PASSAGE_PLAN_AGENT",
    "PASSAGE_PLAN_KEY",
    "PASSAGE_PLAN_MECHANICAL",
    "PASSAGE_PROJECTION_KIND",
    "PASSAGE_SOURCE_TYPES",
    "PassageProjectionResult",
    "passage_entity_id",
    "plan_entity_passages",
    "project_entity_passages",
    "reproject_entity_passages",
    "retire_entity_passages",
    "should_project_passages",
]
