"""Verification helpers for migration archives against the active runtime."""

from __future__ import annotations

from dataclasses import dataclass, field

from sibyl_core.migrate.archive import (
    LoadedArchive,
    effective_graph_counts,
    graph_payload_from_archive,
    validate_archive,
)
from sibyl_core.services.graph_common import normalize_graph_records as normalize_records
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
    expected_episodes: int = 0
    actual_episodes: int = 0
    expected_mentions: int = 0
    actual_mentions: int = 0
    validated_entity_ids: list[str] = field(default_factory=list)
    validated_episode_ids: list[str] = field(default_factory=list)
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
            expected_episodes=0,
            actual_episodes=0,
            expected_mentions=0,
            actual_mentions=0,
            errors=errors,
        )

    expected_counts = effective_graph_counts(graph_payload)
    expected_entities = expected_counts["entity_count"]
    expected_relationships = expected_counts["relationship_count"]
    expected_episodes = expected_counts["episode_count"]
    expected_mentions = expected_counts["mention_count"]

    backup_result = await create_backup(organization_id=organization_id)
    actual_entities = backup_result.entity_count
    actual_relationships = backup_result.relationship_count
    actual_episodes = backup_result.episode_count
    actual_mentions = backup_result.mention_count

    if not backup_result.success:
        errors.append(backup_result.message)

    if actual_entities != expected_entities:
        errors.append(f"entity count mismatch: expected {expected_entities}, got {actual_entities}")
    if actual_relationships != expected_relationships:
        errors.append(
            "relationship count mismatch: "
            f"expected {expected_relationships}, got {actual_relationships}"
        )
    if actual_episodes != expected_episodes:
        errors.append(
            f"episode count mismatch: expected {expected_episodes}, got {actual_episodes}"
        )
    if actual_mentions != expected_mentions:
        errors.append(
            f"mention count mismatch: expected {expected_mentions}, got {actual_mentions}"
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

    validated_episode_ids: list[str] = []
    for episode_payload in list(graph_payload.get("episodes", []))[:sample_size]:
        episode_id = str(episode_payload.get("uuid") or "")
        if not episode_id:
            continue
        # Episodes live in the `episode` table, separate from `entity`. The
        # archive's episode uuid is preserved verbatim in the `uuid` column
        # on import, but the Surreal record id is assigned fresh — so going
        # through `entity_manager.get` (which only queries `entity`) misses
        # every imported episode. Query the episode table directly.
        try:
            rows = normalize_records(
                await runtime.client.execute_query(
                    "SELECT uuid, group_id FROM episode"
                    " WHERE group_id = $group_id AND uuid = $uuid LIMIT 1;",
                    group_id=organization_id,
                    uuid=episode_id,
                )
            )
        except Exception:
            rows = []
        if not rows:
            errors.append(f"missing imported episode: {episode_id}")
            continue
        validated_episode_ids.append(episode_id)

    return GraphVerificationResult(
        success=not errors,
        organization_id=organization_id,
        expected_entities=expected_entities,
        actual_entities=actual_entities,
        expected_relationships=expected_relationships,
        actual_relationships=actual_relationships,
        expected_episodes=expected_episodes,
        actual_episodes=actual_episodes,
        expected_mentions=expected_mentions,
        actual_mentions=actual_mentions,
        validated_entity_ids=validated_entity_ids,
        validated_episode_ids=validated_episode_ids,
        errors=errors,
    )


__all__ = ["GraphVerificationResult", "verify_graph_archive"]
