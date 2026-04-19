"""Verification helpers for migration archives against the active runtime."""

from __future__ import annotations

from dataclasses import dataclass, field

from sibyl_core.migrate.archive import LoadedArchive, graph_payload_from_archive, validate_archive
from sibyl_core.services.graph_runtime import get_graph_runtime
from sibyl_core.tools.admin import create_backup


@dataclass(frozen=True)
class GraphVerificationResult:
    """Verification summary for one graph archive against the active runtime."""

    success: bool
    organization_id: str
    expected_entities: int
    actual_entities: int
    expected_relationships: int
    actual_relationships: int
    validated_entity_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def verify_graph_archive(
    archive: LoadedArchive,
    *,
    organization_id: str,
    sample_size: int = 10,
) -> GraphVerificationResult:
    errors = list(validate_archive(archive))
    graph_payload = graph_payload_from_archive(archive)
    if graph_payload is None:
        errors.append("archive does not contain graph.json")
        return GraphVerificationResult(
            success=False,
            organization_id=organization_id,
            expected_entities=0,
            actual_entities=0,
            expected_relationships=0,
            actual_relationships=0,
            errors=errors,
        )

    expected_entities = int(
        graph_payload.get("entity_count") or len(graph_payload.get("entities", []))
    )
    expected_relationships = int(
        graph_payload.get("relationship_count") or len(graph_payload.get("relationships", []))
    )

    backup_result = await create_backup(organization_id=organization_id)
    actual_entities = backup_result.entity_count
    actual_relationships = backup_result.relationship_count

    if not backup_result.success:
        errors.append(backup_result.message)

    if actual_entities != expected_entities:
        errors.append(
            f"entity count mismatch: expected {expected_entities}, got {actual_entities}"
        )
    if actual_relationships != expected_relationships:
        errors.append(
            "relationship count mismatch: "
            f"expected {expected_relationships}, got {actual_relationships}"
        )

    runtime = await get_graph_runtime(organization_id)
    validated_entity_ids: list[str] = []
    for entity_payload in list(graph_payload.get("entities", []))[:sample_size]:
        entity_id = str(entity_payload.get("id") or "")
        if not entity_id:
            continue
        try:
            entity = await runtime.entity_manager.get(entity_id)
        except Exception:
            entity = None
        if entity is None:
            errors.append(f"missing imported entity: {entity_id}")
            continue
        validated_entity_ids.append(entity_id)

    return GraphVerificationResult(
        success=not errors,
        organization_id=organization_id,
        expected_entities=expected_entities,
        actual_entities=actual_entities,
        expected_relationships=expected_relationships,
        actual_relationships=actual_relationships,
        validated_entity_ids=validated_entity_ids,
        errors=errors,
    )


__all__ = ["GraphVerificationResult", "verify_graph_archive"]
