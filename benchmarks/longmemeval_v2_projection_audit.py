#!/usr/bin/env python3
"""Audit LongMemEval-V2 operational projections without provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from longmemeval_v2_memory.sibyl_memory import build_operational_experience_payload
from sibyl_core.models import OperationalExperience, OperationalExperienceProjection
from sibyl_core.models.entities import EntityType, RelationshipType
from sibyl_core.projection import project_operational_experience

AUDIT_SCHEMA_VERSION = "sibyl-longmemeval-v2-projection-audit-v1"


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
    counts: Counter[str] = Counter()
    entity_types: Counter[str] = Counter()
    relationship_types: Counter[str] = Counter()
    issues: list[dict[str, object]] = []
    max_evidence_chars = 0
    max_entity_chars = 0
    max_embeddable_entity_chars = 0
    max_total_write_amplification = 0.0
    trajectories_above_2x = 0

    for trajectory_index, trajectory_raw in enumerate(iter_jsonl(path)):
        if limit is not None and trajectory_index >= limit:
            break
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
        counts["trajectories"] += 1
        counts["observations"] += len(experience.observations)
        counts["entities"] += len(projection.entities)
        counts["relationships"] += len(projection.relationships)
        if projection_signature(projection) != projection_signature(replay):
            _issue(issues, trajectory_id, "projection_not_deterministic")

        entities_by_id = {entity.id: entity for entity in projection.entities}
        raw_entities = {
            (str(entity.metadata.get("source_observation_id")), str(entity.metadata.get("evidence_part_id"))): entity
            for entity in projection.entities
            if entity.metadata.get("projection_kind") == "raw_observation"
        }
        derived_entities = [
            entity
            for entity in projection.entities
            if entity.entity_type in {EntityType.EVENT, EntityType.PROCEDURE, EntityType.ERROR_PATTERN}
        ]
        derived_support = {
            relationship.source_id
            for relationship in projection.relationships
            if relationship.relationship_type is RelationshipType.DERIVED_FROM
            and relationship.target_id in entities_by_id
            and entities_by_id[relationship.target_id].metadata.get("projection_kind")
            == "raw_observation"
        }

        for entity in projection.entities:
            entity_types[entity.entity_type.value] += 1
            max_entity_chars = max(max_entity_chars, len(entity.content or ""))
            if entity.entity_type is not EntityType.ARTIFACT:
                max_embeddable_entity_chars = max(
                    max_embeddable_entity_chars,
                    len(entity.content or ""),
                )
        for relationship in projection.relationships:
            relationship_types[relationship.relationship_type.value] += 1

        raw_count = len(raw_entities)
        total_without_manifest = len(projection.entities) - 1
        amplification = total_without_manifest / max(raw_count, 1)
        max_total_write_amplification = max(max_total_write_amplification, amplification)
        counts["raw_entities"] += raw_count
        counts["derived_entities"] += len(derived_entities)
        counts["manifest_entities"] += 1
        if amplification > 2.0:
            trajectories_above_2x += 1

        for observation_index, observation in enumerate(experience.observations):
            for evidence in observation.evidence:
                counts["evidence_parts"] += 1
                max_evidence_chars = max(max_evidence_chars, len(evidence.content))
                raw_entity = raw_entities.get((observation.id, evidence.id))
                if raw_entity is None:
                    _issue(
                        issues,
                        trajectory_id,
                        "missing_raw_evidence_entity",
                        observation_id=observation.id,
                        evidence_id=evidence.id,
                    )
                elif f"Evidence:\n{evidence.content}" not in (raw_entity.content or ""):
                    _issue(
                        issues,
                        trajectory_id,
                        "raw_evidence_not_byte_exact",
                        observation_id=observation.id,
                        evidence_id=evidence.id,
                    )
                if len(evidence.content) > content_max_chars:
                    _issue(
                        issues,
                        trajectory_id,
                        "evidence_part_exceeds_limit",
                        observation_id=observation.id,
                        evidence_id=evidence.id,
                        observed=len(evidence.content),
                    )

            if not observation.action:
                continue
            counts["actions"] += 1
            procedures = [
                entity
                for entity in derived_entities
                if entity.entity_type is EntityType.PROCEDURE
            ]
            if not any(observation.action in (entity.content or "") for entity in procedures):
                _issue(
                    issues,
                    trajectory_id,
                    "procedure_missing_exact_action",
                    observation_id=observation.id,
                )
            if observation_index == 0:
                continue
            matching_events = [
                entity
                for entity in derived_entities
                if entity.entity_type is EntityType.EVENT
                and observation.id in entity.metadata.get("source_observation_ids", [])
            ]
            if not any(observation.action in (entity.content or "") for entity in matching_events):
                _issue(
                    issues,
                    trajectory_id,
                    "transition_missing_exact_action",
                    observation_id=observation.id,
                )

        unsupported = [entity.id for entity in derived_entities if entity.id not in derived_support]
        if unsupported:
            _issue(
                issues,
                trajectory_id,
                "derived_entity_without_raw_support",
                entity_ids=unsupported,
            )
        unsupported_claims = [
            entity.id
            for entity in projection.entities
            if entity.entity_type is EntityType.CLAIM
        ]
        if unsupported_claims:
            _issue(
                issues,
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
                issues,
                trajectory_id,
                "unsupported_semantic_relationship",
                relationship_ids=forbidden_relationships,
            )

    total_write_amplification = (
        (counts["raw_entities"] + counts["derived_entities"])
        / max(counts["raw_entities"], 1)
    )
    if total_write_amplification > 2.0:
        _issue(
            issues,
            "corpus",
            "aggregate_write_amplification_above_2x",
            observed=total_write_amplification,
        )
    counts["trajectories_above_2x"] = trajectories_above_2x
    counts["issues"] = len(issues)
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "source": {
            "path": str(path),
            "sha256": sha256_file(path),
            "content_max_chars": content_max_chars,
            "limit": limit,
        },
        "passed": not issues,
        "counts": dict(sorted(counts.items())),
        "entity_types": dict(sorted(entity_types.items())),
        "relationship_types": dict(sorted(relationship_types.items())),
        "bounds": {
            "max_evidence_part_chars": max_evidence_chars,
            "max_projected_entity_chars": max_entity_chars,
            "max_embeddable_entity_chars": max_embeddable_entity_chars,
            "max_total_write_amplification": max_total_write_amplification,
            "aggregate_write_amplification": total_write_amplification,
        },
        "issues": issues,
    }


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected object at {path}:{line_number}")
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
