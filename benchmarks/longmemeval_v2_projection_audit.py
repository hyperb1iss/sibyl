#!/usr/bin/env python3
"""Audit LongMemEval-V2 operational projections without provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BENCHMARKS_ROOT = Path(__file__).resolve().parent
if str(BENCHMARKS_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS_ROOT))

from longmemeval_v2_memory.sibyl_memory import (  # noqa: E402
    build_operational_experience_payload,
)

from sibyl_core.embeddings import entity_embedding_text  # noqa: E402
from sibyl_core.models import (  # noqa: E402
    Entity,
    EntityType,
    OperationalExperience,
    OperationalExperienceProjection,
    RelationshipType,
)
from sibyl_core.projection import project_operational_experience  # noqa: E402

AUDIT_SCHEMA_VERSION = "sibyl-longmemeval-v2-projection-audit-v2"

# Inferred rows per byte-exact raw evidence row. The projector's structural
# ceiling is exactly one inferred row per raw row: one transition event per
# action, one procedure segment, and at most one error pattern, against one raw
# row per evidence part. 2.0 is that ceiling, so anything above it is the
# extractor minting rows no source row accounts for.
MAX_WRITE_AMPLIFICATION = 2.0

# Passage embedding bytes per byte of the raw evidence rows they were cut from.
# Passages are not inference: the slicer partitions a state body's lines
# exactly, so a passage re-carves bytes its parent already holds. Counting them
# against MAX_WRITE_AMPLIFICATION would compare a re-carving to an invention and
# reject the substrate by construction (measured fan-out is 11x to 14.5x rows).
#
# Bytes, not rows, because embedding spend is what this substrate buys: the
# capture path makes no per-entity model call, so the per-row cost is embedding
# tokens. Each passage also carries non-content bytes (entity type, name, the
# 500-char-capped description, the 120-char-capped header, the breadcrumb),
# which is what makes a runaway show up here at all. That per-row floor is
# framing-dependent rather than structural, measured between 101 and 622 chars,
# so this bounds rows only loosely and _audit_passage_spans carries the rest.
#
# 2.5 against amplification that rises with nesting depth, since breadcrumbs are
# uncapped: 1.22x at depth 10, 1.65x at depth 24, 2.20x at depth 48. The A1
# corpus measures 1.65x enterprise / 1.55x web, and the deepest nesting in the
# corpus artifacts is 10, so the real substrate sits at the bottom of that
# curve. A slicer splitting every slice eight ways measures 5.70x and one
# emitting a passage per line 7.17x. The bound therefore clears the corpus by
# roughly 2x while still catching a runaway; a synthetic tree nested past ~48
# would exceed it, which is five times anything the corpus contains.
MAX_PASSAGE_BYTE_AMPLIFICATION = 2.5


@dataclass
class AuditAccumulator:
    counts: Counter[str] = field(default_factory=Counter)
    entity_types: Counter[str] = field(default_factory=Counter)
    relationship_types: Counter[str] = field(default_factory=Counter)
    issues: list[dict[str, object]] = field(default_factory=list)
    max_evidence_chars: int = 0
    max_entity_chars: int = 0
    max_embeddable_entity_chars: int = 0
    max_write_amplification: float = 0.0
    max_passage_byte_amplification: float = 0.0
    max_passage_fanout: float = 0.0
    trajectories_above_write_limit: int = 0
    trajectories_above_passage_limit: int = 0


@dataclass(frozen=True)
class ProjectionIndex:
    """The projected rows split by the account each one belongs to."""

    raw_by_evidence: dict[tuple[str, str], Entity]
    raw_by_id: dict[str, Entity]
    derived: list[Entity]
    passages: list[Entity]
    passages_by_evidence: dict[tuple[str, str], list[Entity]]
    raw_supported: set[str]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    trajectories_path = Path(args.trajectories).expanduser().resolve()
    report = audit_trajectories(
        trajectories_path,
        content_max_chars=args.content_max_chars,
        limit=args.limit,
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
    return 0 if report["passed"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectories", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--content-max-chars", type=int, default=18_000)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    if args.content_max_chars <= 0:
        parser.error("--content-max-chars must be positive")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    return args


def audit_trajectories(
    path: Path,
    *,
    content_max_chars: int,
    limit: int | None = None,
) -> dict[str, Any]:
    audit = AuditAccumulator()
    for trajectory_index, trajectory_raw in enumerate(iter_jsonl(path)):
        if limit is not None and trajectory_index >= limit:
            break
        _audit_trajectory(
            audit,
            trajectory_raw,
            trajectory_index=trajectory_index,
            content_max_chars=content_max_chars,
        )

    aggregate_write_amplification = (
        audit.counts["raw_entities"] + audit.counts["derived_entities"]
    ) / max(audit.counts["raw_entities"], 1)
    if aggregate_write_amplification > MAX_WRITE_AMPLIFICATION:
        _issue(
            audit.issues,
            "corpus",
            "aggregate_write_amplification_above_limit",
            observed=aggregate_write_amplification,
            limit=MAX_WRITE_AMPLIFICATION,
        )
    aggregate_passage_byte_amplification = audit.counts["passage_embedding_chars"] / max(
        audit.counts["passage_parent_embedding_chars"], 1
    )
    if aggregate_passage_byte_amplification > MAX_PASSAGE_BYTE_AMPLIFICATION:
        _issue(
            audit.issues,
            "corpus",
            "aggregate_passage_byte_amplification_above_limit",
            observed=aggregate_passage_byte_amplification,
            limit=MAX_PASSAGE_BYTE_AMPLIFICATION,
        )
    audit.counts["trajectories_above_write_limit"] = audit.trajectories_above_write_limit
    audit.counts["trajectories_above_passage_limit"] = audit.trajectories_above_passage_limit
    audit.counts["issues"] = len(audit.issues)
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "source": {
            "path": str(path),
            "sha256": sha256_file(path),
            "content_max_chars": content_max_chars,
            "limit": limit,
        },
        "passed": not audit.issues,
        "counts": dict(sorted(audit.counts.items())),
        "entity_types": dict(sorted(audit.entity_types.items())),
        "relationship_types": dict(sorted(audit.relationship_types.items())),
        "bounds": {
            "max_evidence_part_chars": audit.max_evidence_chars,
            "max_projected_entity_chars": audit.max_entity_chars,
            "max_embeddable_entity_chars": audit.max_embeddable_entity_chars,
            "max_write_amplification": audit.max_write_amplification,
            "aggregate_write_amplification": aggregate_write_amplification,
            "max_passage_byte_amplification": audit.max_passage_byte_amplification,
            "aggregate_passage_byte_amplification": aggregate_passage_byte_amplification,
            # Reported, not gated. Row fan-out swings with tree shape, and the
            # per-passage byte floor already bounds it; this is the budget
            # signal a corpus rebuild is priced against.
            "max_passage_fanout": audit.max_passage_fanout,
            "aggregate_passage_fanout": audit.counts["passage_entities"]
            / max(audit.counts["raw_entities"], 1),
        },
        "issues": audit.issues,
    }


def _audit_trajectory(
    audit: AuditAccumulator,
    trajectory_raw: dict[str, Any],
    *,
    trajectory_index: int,
    content_max_chars: int,
) -> None:
    trajectory_id = str(trajectory_raw.get("id") or f"row-{trajectory_index + 1}")
    payload = build_operational_experience_payload(
        trajectory_raw,
        project_id="projection-audit",
        run_id="projection-audit",
        content_max_chars=content_max_chars,
    )
    experience = OperationalExperience.model_validate(payload["experience"])
    projection = project_operational_experience(experience)
    replay = project_operational_experience(experience)
    if projection_signature(projection) != projection_signature(replay):
        _issue(audit.issues, trajectory_id, "projection_not_deterministic")

    index = _projection_indexes(projection)
    _record_projection_stats(audit, experience, projection, index)
    _audit_observations(
        audit,
        trajectory_id,
        experience,
        index,
        content_max_chars=content_max_chars,
    )
    _audit_derived_support(audit, trajectory_id, projection, index)


def _projection_indexes(projection: OperationalExperienceProjection) -> ProjectionIndex:
    entities_by_id = {entity.id: entity for entity in projection.entities}
    raw_by_id = {
        entity.id: entity
        for entity in projection.entities
        if entity.metadata.get("projection_kind") == "raw_observation"
    }
    raw_by_evidence = {
        (
            str(entity.metadata.get("source_observation_id")),
            str(entity.metadata.get("evidence_part_id")),
        ): entity
        for entity in raw_by_id.values()
    }
    derived_entities = [
        entity
        for entity in projection.entities
        if entity.entity_type in {EntityType.EVENT, EntityType.PROCEDURE, EntityType.ERROR_PATTERN}
    ]
    passages = [
        entity for entity in projection.entities if entity.entity_type is EntityType.PASSAGE
    ]
    passages_by_evidence: dict[tuple[str, str], list[Entity]] = {}
    for passage in passages:
        key = (
            str(passage.metadata.get("source_observation_id")),
            str(passage.metadata.get("evidence_part_id")),
        )
        passages_by_evidence.setdefault(key, []).append(passage)
    raw_supported = {
        relationship.source_id
        for relationship in projection.relationships
        if relationship.relationship_type is RelationshipType.DERIVED_FROM
        and relationship.target_id in entities_by_id
        and entities_by_id[relationship.target_id].metadata.get("projection_kind")
        == "raw_observation"
    }
    return ProjectionIndex(
        raw_by_evidence=raw_by_evidence,
        raw_by_id=raw_by_id,
        derived=derived_entities,
        passages=passages,
        passages_by_evidence=passages_by_evidence,
        raw_supported=raw_supported,
    )


def _record_projection_stats(
    audit: AuditAccumulator,
    experience: OperationalExperience,
    projection: OperationalExperienceProjection,
    index: ProjectionIndex,
) -> None:
    audit.counts["trajectories"] += 1
    audit.counts["observations"] += len(experience.observations)
    audit.counts["entities"] += len(projection.entities)
    audit.counts["relationships"] += len(projection.relationships)
    for entity in projection.entities:
        audit.entity_types[entity.entity_type.value] += 1
        audit.max_entity_chars = max(audit.max_entity_chars, len(entity.content or ""))
        if entity.entity_type is not EntityType.ARTIFACT:
            audit.max_embeddable_entity_chars = max(
                audit.max_embeddable_entity_chars,
                len(entity.content or ""),
            )
    for relationship in projection.relationships:
        audit.relationship_types[relationship.relationship_type.value] += 1

    raw_count = len(index.raw_by_evidence)
    amplification = (raw_count + len(index.derived)) / max(raw_count, 1)
    audit.max_write_amplification = max(audit.max_write_amplification, amplification)
    audit.counts["raw_entities"] += raw_count
    audit.counts["derived_entities"] += len(index.derived)
    audit.counts["manifest_entities"] += 1
    if amplification > MAX_WRITE_AMPLIFICATION:
        audit.trajectories_above_write_limit += 1

    _record_passage_stats(audit, index)


def _record_passage_stats(audit: AuditAccumulator, index: ProjectionIndex) -> None:
    """Account the re-carved rows against the raw rows they were cut from."""
    if not index.passages:
        return
    parents = [
        index.raw_by_id[parent_id]
        for parent_id in {
            str(passage.metadata.get("parent_entity_id")) for passage in index.passages
        }
        if parent_id in index.raw_by_id
    ]
    passage_chars = sum(len(entity_embedding_text(passage)) for passage in index.passages)
    parent_chars = sum(len(entity_embedding_text(parent)) for parent in parents)
    byte_amplification = passage_chars / max(parent_chars, 1)
    fanout = len(index.passages) / max(len(parents), 1)
    audit.max_passage_byte_amplification = max(
        audit.max_passage_byte_amplification,
        byte_amplification,
    )
    audit.max_passage_fanout = max(audit.max_passage_fanout, fanout)
    audit.counts["passage_entities"] += len(index.passages)
    audit.counts["passage_embedding_chars"] += passage_chars
    audit.counts["passage_parent_embedding_chars"] += parent_chars
    if byte_amplification > MAX_PASSAGE_BYTE_AMPLIFICATION:
        audit.trajectories_above_passage_limit += 1


def _audit_observations(
    audit: AuditAccumulator,
    trajectory_id: str,
    experience: OperationalExperience,
    index: ProjectionIndex,
    *,
    content_max_chars: int,
) -> None:
    procedures = [entity for entity in index.derived if entity.entity_type is EntityType.PROCEDURE]
    for observation_index, observation in enumerate(experience.observations):
        for evidence in observation.evidence:
            audit.counts["evidence_parts"] += 1
            audit.max_evidence_chars = max(
                audit.max_evidence_chars,
                len(evidence.content),
            )
            raw_entity = index.raw_by_evidence.get((observation.id, evidence.id))
            _audit_raw_evidence(
                audit.issues,
                trajectory_id,
                observation.id,
                evidence.id,
                evidence.content,
                raw_entity,
                content_max_chars=content_max_chars,
            )
            _audit_passage_spans(
                audit.issues,
                trajectory_id,
                observation.id,
                evidence.id,
                evidence.content,
                index.passages_by_evidence.get((observation.id, evidence.id), []),
            )

        if not observation.action:
            continue
        audit.counts["actions"] += 1
        if not any(observation.action in (entity.content or "") for entity in procedures):
            _issue(
                audit.issues,
                trajectory_id,
                "procedure_missing_exact_action",
                observation_id=observation.id,
            )
        if observation_index > 0:
            _audit_transition_action(
                audit.issues,
                trajectory_id,
                observation.id,
                observation.action,
                index.derived,
            )


def _audit_passage_spans(
    issues: list[dict[str, object]],
    trajectory_id: str,
    observation_id: str,
    evidence_id: str,
    evidence_content: str,
    passages: list[Entity],
) -> None:
    """Hold passages to the partition the slicer promises.

    Excluding passages from the invention bound is only sound while they really
    are a re-carving of their parent, so the claim is checked rather than
    assumed: every span lands inside the parent body, carries that body's own
    bytes, and no line is claimed twice. Without this a slicer could mint
    unbounded rows that discard the content they name, and the byte bound would
    read it as thrift.
    """
    if not passages:
        return
    details = {"observation_id": observation_id, "evidence_id": evidence_id}
    lines = evidence_content.split("\n")
    claimed: set[int] = set()
    for passage in passages:
        start = passage.metadata.get("passage_line_start")
        end = passage.metadata.get("passage_line_end")
        if not isinstance(start, int) or not isinstance(end, int):
            _issue(issues, trajectory_id, "passage_span_missing", **details, entity_id=passage.id)
            continue
        if not 0 <= start <= end < len(lines):
            _issue(
                issues,
                trajectory_id,
                "passage_span_outside_parent",
                **details,
                entity_id=passage.id,
                observed=[start, end],
            )
            continue
        content = passage.content or ""
        if any(lines[edge] not in content for edge in (start, end)):
            _issue(
                issues,
                trajectory_id,
                "passage_not_byte_exact",
                **details,
                entity_id=passage.id,
            )
        overlap = claimed.intersection(range(start, end + 1))
        if overlap:
            _issue(
                issues,
                trajectory_id,
                "passage_span_overlaps_sibling",
                **details,
                entity_id=passage.id,
                observed=sorted(overlap)[:8],
            )
        claimed.update(range(start, end + 1))


def _audit_raw_evidence(
    issues: list[dict[str, object]],
    trajectory_id: str,
    observation_id: str,
    evidence_id: str,
    evidence_content: str,
    raw_entity: Entity | None,
    *,
    content_max_chars: int,
) -> None:
    details = {"observation_id": observation_id, "evidence_id": evidence_id}
    if raw_entity is None:
        _issue(issues, trajectory_id, "missing_raw_evidence_entity", **details)
    elif f"Evidence:\n{evidence_content}" not in (raw_entity.content or ""):
        _issue(issues, trajectory_id, "raw_evidence_not_byte_exact", **details)
    if len(evidence_content) > content_max_chars:
        _issue(
            issues,
            trajectory_id,
            "evidence_part_exceeds_limit",
            **details,
            observed=len(evidence_content),
        )


def _audit_transition_action(
    issues: list[dict[str, object]],
    trajectory_id: str,
    observation_id: str,
    action: str,
    derived_entities: list[Entity],
) -> None:
    matching_events = [
        entity
        for entity in derived_entities
        if entity.entity_type is EntityType.EVENT
        and observation_id in entity.metadata.get("source_observation_ids", [])
    ]
    if not any(action in (entity.content or "") for entity in matching_events):
        _issue(
            issues,
            trajectory_id,
            "transition_missing_exact_action",
            observation_id=observation_id,
        )


def _audit_derived_support(
    audit: AuditAccumulator,
    trajectory_id: str,
    projection: OperationalExperienceProjection,
    index: ProjectionIndex,
) -> None:
    unsupported = [entity.id for entity in index.derived if entity.id not in index.raw_supported]
    if unsupported:
        _issue(
            audit.issues,
            trajectory_id,
            "derived_entity_without_raw_support",
            entity_ids=unsupported,
        )
    orphan_passages = [
        passage.id
        for passage in index.passages
        if passage.id not in index.raw_supported
        or str(passage.metadata.get("parent_entity_id")) not in index.raw_by_id
    ]
    if orphan_passages:
        _issue(
            audit.issues,
            trajectory_id,
            "passage_without_raw_parent",
            entity_ids=orphan_passages,
        )
    unsupported_claims = [
        entity.id for entity in projection.entities if entity.entity_type is EntityType.CLAIM
    ]
    if unsupported_claims:
        _issue(
            audit.issues,
            trajectory_id,
            "unsupported_claim_entity",
            entity_ids=unsupported_claims,
        )
    forbidden_relationships = [
        relationship.id
        for relationship in projection.relationships
        if relationship.relationship_type is RelationshipType.CONTRADICTS
    ]
    if forbidden_relationships:
        _issue(
            audit.issues,
            trajectory_id,
            "unsupported_semantic_relationship",
            relationship_ids=forbidden_relationships,
        )


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"Expected object at {path}:{line_number}")
            yield value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def projection_signature(projection: OperationalExperienceProjection) -> str:
    payload = {
        "manifest": projection.manifest.model_dump(mode="json"),
        "entities": [
            entity.model_dump(
                mode="json",
                exclude={"created_at", "updated_at"},
            )
            for entity in projection.entities
        ],
        "relationships": [
            relationship.model_dump(
                mode="json",
                exclude={"created_at", "updated_at"},
            )
            for relationship in projection.relationships
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _issue(
    issues: list[dict[str, object]],
    trajectory_id: str,
    code: str,
    **details: object,
) -> None:
    issues.append({"trajectory_id": trajectory_id, "code": code, **details})


if __name__ == "__main__":
    raise SystemExit(main())
