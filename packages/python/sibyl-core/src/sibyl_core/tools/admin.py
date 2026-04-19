"""Admin tools for the Conventions MCP Server.

Provides maintenance and diagnostic capabilities.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog

from sibyl_core.config import settings
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import (
    count_entities_by_type,
    get_graph_client,
    get_graph_runtime,
)

log = structlog.get_logger()
get_legacy_graph_client = get_graph_client
get_legacy_graph_runtime = get_graph_runtime

BACKFILL_PAGE_SIZE = 1000


@dataclass
class HealthStatus:
    """Server health status."""

    status: str  # "healthy", "degraded", "unhealthy"
    server_name: str
    uptime_seconds: float
    graph_connected: bool
    entity_counts: dict[str, int] = field(default_factory=dict)
    search_latency_ms: float | None = None
    last_sync: datetime | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class RebuildResult:
    """Result of an index rebuild operation."""

    success: bool
    indices_rebuilt: list[str]
    duration_seconds: float
    message: str


@dataclass
class ServerState:
    """Tracks server runtime state."""

    start_time: float | None = None


# Singleton instance for server state
_state = ServerState()


def mark_server_started() -> None:
    """Mark the server as started for uptime tracking."""
    _state.start_time = time.time()


async def health_check(*, organization_id: str | None = None) -> HealthStatus:
    """Check server health and return status.

    Performs health checks on:
    - Graph database connectivity
    - Entity counts by type (if organization_id provided)
    - Search latency (if organization_id provided)

    Args:
        organization_id: Organization ID for graph operations. If None, only basic
                        connectivity is checked.

    Returns:
        HealthStatus with current server state.
    """
    log.info("Performing health check")

    errors: list[str] = []
    graph_connected = False
    entity_counts: dict[str, int] = {}
    search_latency_ms: float | None = None

    # Calculate uptime
    uptime = 0.0
    if _state.start_time is not None:
        uptime = time.time() - _state.start_time

    try:
        if organization_id:
            runtime = await get_legacy_graph_runtime(organization_id)
            entity_manager = runtime.entity_manager
            graph_connected = True
            for entity_type in EntityType:
                entity_counts[entity_type.value] = -1

            try:
                entity_counts = await count_entities_by_type(entity_manager)
            except Exception as e:
                errors.append(f"Entity count query failed: {e}")

            # Test search latency
            try:
                start = time.time()
                await entity_manager.search(
                    query="test", entity_types=[EntityType.PATTERN], limit=1
                )
                search_latency_ms = (time.time() - start) * 1000
            except Exception as e:
                errors.append(f"Search latency test failed: {e}")
        else:
            await get_legacy_graph_client()
            graph_connected = True

    except Exception as e:
        errors.append(f"Graph connection failed: {e}")

    # Determine overall status
    if not graph_connected:
        status = "unhealthy"
    elif errors:
        status = "degraded"
    else:
        status = "healthy"

    return HealthStatus(
        status=status,
        server_name=settings.server_name,
        uptime_seconds=uptime,
        graph_connected=graph_connected,
        entity_counts=entity_counts,
        search_latency_ms=search_latency_ms,
        last_sync=None,  # TODO: Track last sync time
        errors=errors,
    )


async def rebuild_indices(
    index_type: str | None = None,
) -> RebuildResult:
    """Rebuild graph indices for better query performance.

    Args:
        index_type: Specific index to rebuild. Options:
            - "search": Rebuild search/embedding indices
            - "relationships": Rebuild relationship indices
            - "all": Rebuild all indices (default)

    Returns:
        RebuildResult with rebuild status.
    """
    log.info("Rebuilding indices", index_type=index_type)

    start_time = time.time()
    indices_rebuilt: list[str] = []
    target = (index_type or "all").strip().lower()
    valid_targets = {"search", "relationships", "all"}

    if target not in valid_targets:
        return RebuildResult(
            success=False,
            indices_rebuilt=[],
            duration_seconds=time.time() - start_time,
            message=(
                f"Unknown index type: {target}. Valid options are: search, relationships, all."
            ),
        )

    log.warning(
        "index_rebuild_not_implemented",
        index_type=target,
    )
    return RebuildResult(
        success=False,
        indices_rebuilt=indices_rebuilt,
        duration_seconds=time.time() - start_time,
        message=(
            "Index rebuild is not implemented for the current FalkorDB/Graphiti runtime. "
            f"Requested target: {target}."
        ),
    )


async def get_stats(*, organization_id: str | None = None) -> dict[str, object]:
    """Get detailed statistics about the knowledge graph.

    Args:
        organization_id: Organization ID for graph operations. If None, returns minimal stats.

    Returns:
        Dictionary with graph statistics.
    """
    log.info("Getting graph stats")

    stats: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "entities": {},
        "relationships": {},
        "storage": {},
    }

    if not organization_id:
        return stats

    try:
        runtime = await get_legacy_graph_runtime(organization_id)

        entity_stats = await count_entities_by_type(runtime.entity_manager)

        stats["entities"] = entity_stats
        stats["total_entities"] = sum(entity_stats.values())

        # TODO: Add relationship stats from RelationshipManager
        # TODO: Add storage stats from Graphiti

        return stats

    except Exception as e:
        log.error("Failed to get stats", error=str(e))
        return {
            "error": str(e),
            "timestamp": datetime.now(UTC).isoformat(),
        }


@dataclass
class MigrationResult:
    """Result of a migration operation."""

    success: bool
    entities_updated: int
    message: str
    duration_seconds: float


async def _cast_name_embeddings_to_vecf32(
    client: Any,
    *,
    batch_size: int,
    max_entities: int,
) -> int:
    entities_updated = 0
    offset = 0
    scanned = 0

    while scanned < max_entities:
        query = """
            MATCH (n)
            WHERE (n:Entity OR n:Community)
              AND n.name_embedding IS NOT NULL
            RETURN n.uuid AS uuid
            ORDER BY uuid
            SKIP $offset
            LIMIT $limit
            """
        result = await client.driver.execute_query(query, offset=offset, limit=batch_size)

        records = result[0] if result and len(result) > 0 else []
        if not records:
            break

        uuids = [r.get("uuid") for r in records if isinstance(r, dict) and r.get("uuid")]
        scanned += len(uuids)

        for uuid in uuids:
            try:
                cast_query = """
                    MATCH (n {uuid: $uuid})
                    SET n.name_embedding = vecf32(n.name_embedding)
                    RETURN n.uuid AS uuid
                    """
                await client.driver.execute_query(cast_query, uuid=uuid)
                entities_updated += 1
            except Exception as e:
                # Already Vectorf32 (expected), skip silently.
                if "expected List or Null but was Vectorf32" in str(e):
                    continue
                log.warning("embedding_cast_failed", uuid=uuid, error=str(e))

        offset += batch_size

    return entities_updated


async def _clear_mismatched_name_embedding_dimensions(
    client: Any,
    *,
    expected_dim: int,
    batch_size: int,
    max_entities: int,
) -> int:
    embeddings_cleared = 0
    offset = 0
    scanned = 0

    while scanned < max_entities:
        query = """
            MATCH (n)
            WHERE (n:Entity OR n:Community)
              AND n.name_embedding IS NOT NULL
            RETURN n.uuid AS uuid, n.name_embedding AS emb
            ORDER BY uuid
            SKIP $offset
            LIMIT $limit
            """
        result = await client.driver.execute_query(query, offset=offset, limit=batch_size)

        records = result[0] if result and len(result) > 0 else []
        if not records:
            break

        scanned += len(records)
        for record in records:
            if not isinstance(record, dict):
                continue

            uuid = record.get("uuid")
            emb = record.get("emb")
            if not uuid or emb is None:
                continue

            if isinstance(emb, list):
                dim = len(emb)
            elif isinstance(emb, str):
                dim = len([x for x in emb.split(",") if x])
            else:
                dim = None

            if dim is None or dim == expected_dim:
                continue

            try:
                clear_query = """
                    MATCH (n {uuid: $uuid})
                    SET n.name_embedding = NULL
                    RETURN n.uuid AS uuid
                    """
                await client.driver.execute_query(clear_query, uuid=uuid)
                embeddings_cleared += 1
            except Exception as e:
                log.warning("embedding_clear_failed", uuid=uuid, error=str(e))

        offset += batch_size

    return embeddings_cleared


async def migrate_fix_name_embedding_types(
    batch_size: int = 250,
    max_entities: int = 20_000,
) -> MigrationResult:
    """Fix nodes with list-typed `name_embedding` by casting to Vectorf32.

    FalkorDB vector functions (vec.cosineDistance) expect Vectorf32. Some
    legacy writes stored `name_embedding` as a plain List[float], which breaks
    vector queries and can cascade into unrelated flows (e.g. auto-link search).

    We detect list-typed embeddings opportunistically by attempting:
        SET n.name_embedding = vecf32(n.name_embedding)
    This succeeds for list embeddings and fails (with a type mismatch) for
    nodes that already have Vectorf32. Those are safely skipped.

    Args:
        batch_size: Number of candidate nodes to scan per page.
        max_entities: Safety cap to avoid unbounded scans.

    Returns:
        MigrationResult summarizing how many nodes were updated.
    """
    log.info(
        "Running migration: fix name_embedding types",
        batch_size=batch_size,
        max_entities=max_entities,
    )

    start_time = time.time()

    try:
        client = await get_legacy_graph_client()
        expected_dim = settings.graph_embedding_dimensions

        entities_updated = await _cast_name_embeddings_to_vecf32(
            client,
            batch_size=batch_size,
            max_entities=max_entities,
        )
        embeddings_cleared = await _clear_mismatched_name_embedding_dimensions(
            client,
            expected_dim=expected_dim,
            batch_size=batch_size,
            max_entities=max_entities,
        )

        duration = time.time() - start_time
        return MigrationResult(
            success=True,
            entities_updated=entities_updated + embeddings_cleared,
            message=(
                f"Fixed name_embedding for {entities_updated} node(s) (Vectorf32 cast), "
                f"cleared {embeddings_cleared} mismatched-dimension embedding(s) "
                f"(expected {expected_dim})"
            ),
            duration_seconds=duration,
        )

    except Exception as e:
        log.exception("Migration failed", error=str(e))
        return MigrationResult(
            success=False,
            entities_updated=0,
            message=f"Migration failed: {e}",
            duration_seconds=time.time() - start_time,
        )


@dataclass
class BackupData:
    """Graph backup data structure."""

    version: str
    created_at: str
    organization_id: str
    entity_count: int
    relationship_count: int
    entities: list[dict]
    relationships: list[dict]


@dataclass
class BackupResult:
    """Result of a backup operation."""

    success: bool
    entity_count: int
    relationship_count: int
    backup_data: BackupData | None
    message: str
    duration_seconds: float


@dataclass
class RestoreResult:
    """Result of a restore operation."""

    success: bool
    entities_restored: int
    relationships_restored: int
    entities_skipped: int
    relationships_skipped: int
    errors: list[str]
    duration_seconds: float


# Entity types to include in backup
BACKUP_ENTITY_TYPES = [
    EntityType.PATTERN,
    EntityType.RULE,
    EntityType.TEMPLATE,
    EntityType.TOOL,
    EntityType.LANGUAGE,
    EntityType.TOPIC,
    EntityType.EPISODE,
    EntityType.TASK,
    EntityType.PROJECT,
    EntityType.TEAM,
    EntityType.KNOWLEDGE_SOURCE,
    EntityType.DOCUMENT,
]


async def create_backup(*, organization_id: str) -> BackupResult:
    """Create a backup of all graph data for an organization.

    Args:
        organization_id: Organization UUID to backup.

    Returns:
        BackupResult with backup data or error information.
    """
    log.info("Creating backup", organization_id=organization_id)
    start_time = time.time()

    try:
        runtime = await get_legacy_graph_runtime(organization_id)
        entity_manager = runtime.entity_manager
        relationship_manager = runtime.relationship_manager

        # Collect all entities
        all_entities: list[Entity] = []
        for entity_type in BACKUP_ENTITY_TYPES:
            try:
                entities = await entity_manager.list_by_type(entity_type, limit=10000)
                all_entities.extend(entities)
            except Exception as e:
                log.warning("Failed to backup entity type", type=entity_type.value, error=str(e))

        # Collect all relationships
        relationships = await relationship_manager.list_all(limit=50000)

        # Build backup data
        backup_data = BackupData(
            version="2.0",
            created_at=datetime.now(UTC).isoformat(),
            organization_id=organization_id,
            entity_count=len(all_entities),
            relationship_count=len(relationships),
            entities=[e.model_dump(mode="json") for e in all_entities],
            relationships=[r.model_dump(mode="json") for r in relationships],
        )

        duration = time.time() - start_time
        log.info(
            "Backup created",
            entities=len(all_entities),
            relationships=len(relationships),
            duration=duration,
        )

        return BackupResult(
            success=True,
            entity_count=len(all_entities),
            relationship_count=len(relationships),
            backup_data=backup_data,
            message=f"Backup created: {len(all_entities)} entities, {len(relationships)} relationships",
            duration_seconds=duration,
        )

    except Exception as e:
        log.exception("Backup failed", error=str(e))
        return BackupResult(
            success=False,
            entity_count=0,
            relationship_count=0,
            backup_data=None,
            message=f"Backup failed: {e}",
            duration_seconds=time.time() - start_time,
        )


async def restore_backup(
    backup_data: BackupData,
    *,
    organization_id: str,
    skip_existing: bool = True,
) -> RestoreResult:
    """Restore graph data from a backup.

    Args:
        backup_data: The backup data to restore.
        organization_id: Organization UUID to restore into.
        skip_existing: If True, skip entities/relationships that already exist.

    Returns:
        RestoreResult with restore statistics.
    """
    log.info(
        "Restoring backup",
        organization_id=organization_id,
        entities=backup_data.entity_count,
        relationships=backup_data.relationship_count,
    )
    start_time = time.time()

    errors: list[str] = []
    entities_restored = 0
    entities_skipped = 0
    relationships_restored = 0
    relationships_skipped = 0

    try:
        runtime = await get_legacy_graph_runtime(organization_id)
        entity_manager = runtime.entity_manager
        relationship_manager = runtime.relationship_manager

        # Restore entities
        for entity_data in backup_data.entities:
            try:
                entity = Entity.model_validate(entity_data)
                # Check if entity exists (get() raises on missing)
                if skip_existing:
                    try:
                        existing = await entity_manager.get(entity.id)
                        if existing:
                            entities_skipped += 1
                            continue
                    except Exception:
                        pass  # Entity doesn't exist — proceed to create

                await entity_manager.create(entity)
                entities_restored += 1
            except Exception as e:
                error_msg = f"Entity {entity_data.get('id', 'unknown')}: {e}"
                errors.append(error_msg)
                if len(errors) <= 10:
                    log.warning("Entity restore failed", error=error_msg)

        # Restore relationships
        for rel_data in backup_data.relationships:
            try:
                relationship = Relationship.model_validate(rel_data)
                # Note: RelationshipManager doesn't have get(id), so we can't check existence
                # Create will fail if relationship already exists (handled in except block)
                await relationship_manager.create(relationship)
                relationships_restored += 1
            except Exception as e:
                error_msg = f"Relationship {rel_data.get('id', 'unknown')}: {e}"
                errors.append(error_msg)
                if len(errors) <= 10:
                    log.warning("Relationship restore failed", error=error_msg)

        duration = time.time() - start_time
        log.info(
            "Restore completed",
            entities_restored=entities_restored,
            entities_skipped=entities_skipped,
            relationships_restored=relationships_restored,
            relationships_skipped=relationships_skipped,
            errors=len(errors),
            duration=duration,
        )

        return RestoreResult(
            success=len(errors) == 0,
            entities_restored=entities_restored,
            relationships_restored=relationships_restored,
            entities_skipped=entities_skipped,
            relationships_skipped=relationships_skipped,
            errors=errors[:50],  # Limit error list
            duration_seconds=duration,
        )

    except Exception as e:
        log.exception("Restore failed", error=str(e))
        return RestoreResult(
            success=False,
            entities_restored=entities_restored,
            relationships_restored=relationships_restored,
            entities_skipped=entities_skipped,
            relationships_skipped=relationships_skipped,
            errors=[str(e), *errors[:49]],
            duration_seconds=time.time() - start_time,
        )


@dataclass
class BackfillResult:
    """Result of a relationship backfill operation."""

    success: bool
    relationships_created: int
    tasks_without_project: int
    tasks_already_linked: int
    errors: list[str]
    duration_seconds: float


async def backfill_task_project_relationships(
    *,
    organization_id: str,
    dry_run: bool = False,
) -> BackfillResult:
    """Backfill BELONGS_TO relationships for tasks with project_id in metadata.

    Finds tasks that have a project_id in their metadata but no BELONGS_TO
    relationship edge to that project, and creates the missing edges.

    Args:
        organization_id: Organization UUID to process.
        dry_run: If True, only report what would be done without making changes.

    Returns:
        BackfillResult with statistics about what was processed/created.
    """
    log.info(
        "Backfilling task->project relationships",
        organization_id=organization_id,
        dry_run=dry_run,
    )
    start_time = time.time()

    errors: list[str] = []
    relationships_created = 0
    tasks_without_project = 0
    tasks_already_linked = 0

    try:
        runtime = await get_legacy_graph_runtime(organization_id)
        entity_manager = runtime.entity_manager
        relationship_manager = runtime.relationship_manager

        # Get all tasks in pages so large organizations are fully processed.
        tasks: list[Entity] = []
        task_offset = 0
        while True:
            batch = await entity_manager.list_by_type(
                EntityType.TASK,
                limit=BACKFILL_PAGE_SIZE,
                offset=task_offset,
            )
            if not batch:
                break

            tasks.extend(batch)
            task_offset += BACKFILL_PAGE_SIZE

            if len(batch) < BACKFILL_PAGE_SIZE:
                break

        log.info("Found tasks to process", count=len(tasks))

        # Get all projects for validation.
        project_ids: set[str] = set()
        project_offset = 0
        while True:
            projects = await entity_manager.list_by_type(
                EntityType.PROJECT,
                limit=BACKFILL_PAGE_SIZE,
                offset=project_offset,
                include_archived=True,
            )
            if not projects:
                break

            project_ids.update(p.id for p in projects)
            project_offset += BACKFILL_PAGE_SIZE

            if len(projects) < BACKFILL_PAGE_SIZE:
                break

        log.info("Found projects", count=len(project_ids))

        for task in tasks:
            task_id = task.id
            project_id = task.metadata.get("project_id") if task.metadata else None

            if not project_id:
                tasks_without_project += 1
                continue

            # Validate project exists
            if project_id not in project_ids:
                errors.append(f"Task {task_id}: project {project_id} not found")
                continue

            # Check if BELONGS_TO relationship already exists
            existing_rels = await relationship_manager.get_for_entity(task_id, direction="outgoing")
            has_belongs_to = any(
                r.target_id == project_id and r.relationship_type == RelationshipType.BELONGS_TO
                for r in existing_rels
            )

            if has_belongs_to:
                tasks_already_linked += 1
                continue

            # Create the missing relationship
            if dry_run:
                log.info("Would create BELONGS_TO", task=task_id, project=project_id)
                relationships_created += 1
            else:
                try:
                    rel = Relationship(
                        id=f"rel_{task_id}_belongs_to_{project_id}",
                        source_id=task_id,
                        target_id=project_id,
                        relationship_type=RelationshipType.BELONGS_TO,
                        metadata={"backfilled": True, "created_at": datetime.now(UTC).isoformat()},
                    )
                    await relationship_manager.create(rel)
                    relationships_created += 1
                    log.info("Created BELONGS_TO", task=task_id, project=project_id)
                except Exception as e:
                    errors.append(f"Task {task_id}: {e}")

        duration = time.time() - start_time
        log.info(
            "Backfill completed",
            relationships_created=relationships_created,
            tasks_without_project=tasks_without_project,
            tasks_already_linked=tasks_already_linked,
            errors=len(errors),
            duration=duration,
            dry_run=dry_run,
        )

        return BackfillResult(
            success=len(errors) == 0,
            relationships_created=relationships_created,
            tasks_without_project=tasks_without_project,
            tasks_already_linked=tasks_already_linked,
            errors=errors[:50],
            duration_seconds=duration,
        )

    except Exception as e:
        log.exception("Backfill failed", error=str(e))
        return BackfillResult(
            success=False,
            relationships_created=relationships_created,
            tasks_without_project=tasks_without_project,
            tasks_already_linked=tasks_already_linked,
            errors=[str(e), *errors[:49]],
            duration_seconds=time.time() - start_time,
        )


@dataclass
class ProjectIdBackfillResult:
    """Result of project_id property backfill."""

    success: bool
    nodes_updated: int
    nodes_already_set: int
    nodes_without_project_rel: int
    errors: list[str]
    duration_seconds: float


async def backfill_project_id_from_relationships(
    *,
    organization_id: str,
    dry_run: bool = False,
) -> ProjectIdBackfillResult:
    """Backfill project_id property on nodes based on BELONGS_TO relationships.

    Finds nodes that have BELONGS_TO relationships to projects but are missing
    the project_id property, and sets it based on the relationship target.

    Args:
        organization_id: Organization UUID to process.
        dry_run: If True, only report what would be done without making changes.

    Returns:
        ProjectIdBackfillResult with statistics about what was processed/updated.
    """
    log.info(
        "backfill_project_id_start",
        organization_id=organization_id,
        dry_run=dry_run,
    )
    start_time = time.time()

    errors: list[str] = []
    nodes_updated = 0
    nodes_already_set = 0
    nodes_without_project_rel = 0

    try:
        runtime = await get_legacy_graph_runtime(organization_id)
        entity_manager = runtime.entity_manager
        relationship_manager = runtime.relationship_manager

        project_ids: set[str] = set()
        project_offset = 0
        while True:
            projects = await entity_manager.list_by_type(
                EntityType.PROJECT,
                limit=BACKFILL_PAGE_SIZE,
                offset=project_offset,
                include_archived=True,
            )
            if not projects:
                break
            project_offset += len(projects)
            project_ids.update(
                str(project.id)
                for project in projects
                if getattr(project, "id", None)
            )

        updates_needed: list[tuple[str, str, str]] = []
        entity_offset = 0
        while True:
            entities = await entity_manager.list_all(
                limit=BACKFILL_PAGE_SIZE,
                offset=entity_offset,
                include_archived=True,
            )
            if not entities:
                break

            entity_offset += len(entities)
            for entity in entities:
                node_id = getattr(entity, "id", None)
                if not node_id:
                    continue

                metadata = getattr(entity, "metadata", None)
                metadata = metadata if isinstance(metadata, dict) else {}
                project_id = getattr(entity, "project_id", None) or metadata.get("project_id")
                if project_id:
                    nodes_already_set += 1
                    continue

                relationships = await relationship_manager.get_for_entity(
                    str(node_id),
                    relationship_types=[RelationshipType.BELONGS_TO],
                    direction="outgoing",
                )
                relationship_project_id = next(
                    (
                        relationship.target_id
                        for relationship in relationships
                        if relationship.target_id in project_ids
                    ),
                    None,
                )

                if relationship_project_id:
                    updates_needed.append(
                        (
                            str(node_id),
                            str(relationship_project_id),
                            str(getattr(entity, "name", "") or ""),
                        )
                    )
                else:
                    nodes_without_project_rel += 1

        log.info("backfill_nodes_found", count=len(updates_needed))

        if dry_run:
            for node_id, project_id, node_name in updates_needed:
                log.info(
                    "backfill_would_update",
                    node_id=node_id,
                    node_name=node_name,
                    project_id=project_id,
                )
            nodes_updated = len(updates_needed)
        else:
            for node_id, project_id, node_name in updates_needed:
                try:
                    await entity_manager.update(node_id, {"project_id": project_id})
                    nodes_updated += 1
                    log.debug(
                        "backfill_node_updated",
                        node_id=node_id,
                        node_name=node_name,
                        project_id=project_id,
                    )
                except Exception as e:
                    errors.append(f"Node {node_id}: {e}")

        if not dry_run:
            nodes_already_set += nodes_updated

        duration = time.time() - start_time
        log.info(
            "backfill_project_id_complete",
            nodes_updated=nodes_updated,
            nodes_already_set=nodes_already_set,
            nodes_without_project_rel=nodes_without_project_rel,
            errors=len(errors),
            duration=duration,
            dry_run=dry_run,
        )

        return ProjectIdBackfillResult(
            success=len(errors) == 0,
            nodes_updated=nodes_updated,
            nodes_already_set=nodes_already_set,
            nodes_without_project_rel=nodes_without_project_rel,
            errors=errors[:50],
            duration_seconds=duration,
        )

    except Exception as e:
        log.exception("backfill_project_id_failed", error=str(e))
        return ProjectIdBackfillResult(
            success=False,
            nodes_updated=nodes_updated,
            nodes_already_set=nodes_already_set,
            nodes_without_project_rel=nodes_without_project_rel,
            errors=[str(e), *errors[:49]],
            duration_seconds=time.time() - start_time,
        )


# =============================================================================
# Episode -> Task Relationship Backfill
# =============================================================================


@dataclass
class EpisodeRelationshipBackfillResult:
    """Result of episode -> task relationship backfill."""

    success: bool
    relationships_created: int
    episodes_already_linked: int
    episodes_without_task: int
    errors: list[str]
    duration_seconds: float


async def backfill_episode_task_relationships(
    *,
    organization_id: str,
    dry_run: bool = False,
) -> EpisodeRelationshipBackfillResult:
    """Backfill RELATED_TO relationships from episodes to their referenced tasks.

    Finds episode nodes that have a task_id in their metadata but no
    relationship to that task, and creates RELATED_TO edges.

    Args:
        organization_id: The organization UUID.
        dry_run: If True, only report what would be done without making changes.

    Returns:
        EpisodeRelationshipBackfillResult with counts and any errors.
    """
    import json
    import time

    start_time = time.time()
    relationships_created = 0
    episodes_already_linked = 0
    episodes_without_task = 0
    errors: list[str] = []

    log.info(
        "backfill_episode_task_start",
        organization_id=organization_id,
        dry_run=dry_run,
    )

    try:
        runtime = await get_legacy_graph_runtime(organization_id)
        entity_manager = runtime.entity_manager
        relationship_manager = runtime.relationship_manager

        # Parse metadata and collect episodes with task references
        episodes_to_link: list[tuple[str, str]] = []  # (episode_id, task_id)
        offset = 0
        while True:
            episodes = await entity_manager.list_by_type(
                EntityType.EPISODE,
                limit=BACKFILL_PAGE_SIZE,
                offset=offset,
                include_archived=True,
            )
            if not episodes:
                break

            offset += len(episodes)
            for episode in episodes:
                metadata_raw = getattr(episode, "metadata", None)
                if not metadata_raw:
                    continue

                try:
                    metadata = (
                        json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw
                    )
                except (json.JSONDecodeError, TypeError):
                    continue

                if not isinstance(metadata, dict):
                    continue

                task_id = metadata.get("task_id")
                if task_id and getattr(episode, "id", None):
                    episodes_to_link.append((str(episode.id), str(task_id)))

        log.info("backfill_episodes_with_task_ref", count=len(episodes_to_link))

        # Check which ones need relationships created
        for episode_id, task_id in episodes_to_link:
            relationships = await relationship_manager.get_for_entity(
                episode_id,
                direction="both",
            )
            has_rel = any(
                (relationship.source_id == episode_id and relationship.target_id == task_id)
                or (relationship.source_id == task_id and relationship.target_id == episode_id)
                for relationship in relationships
            )

            if has_rel:
                episodes_already_linked += 1
                continue

            task = await entity_manager.get(task_id)
            if not task:
                episodes_without_task += 1
                continue

            # Create the relationship
            if dry_run:
                log.debug(
                    "would_create_episode_task_rel",
                    episode_id=episode_id,
                    task_id=task_id,
                )
            else:
                try:
                    await relationship_manager.create(
                        Relationship(
                            id=f"rel_{uuid4().hex}",
                            relationship_type=RelationshipType.RELATED_TO,
                            source_id=episode_id,
                            target_id=task_id,
                            metadata={"backfilled": True},
                            created_at=datetime.now(UTC),
                        )
                    )
                    log.debug(
                        "created_episode_task_rel",
                        episode_id=episode_id,
                        task_id=task_id,
                    )
                except Exception as e:
                    errors.append(f"Failed to link {episode_id} -> {task_id}: {e}")
                    continue

            relationships_created += 1

        duration = time.time() - start_time
        log.info(
            "backfill_episode_task_complete",
            relationships_created=relationships_created,
            episodes_already_linked=episodes_already_linked,
            episodes_without_task=episodes_without_task,
            errors=len(errors),
            duration=duration,
            dry_run=dry_run,
        )

        return EpisodeRelationshipBackfillResult(
            success=True,
            relationships_created=relationships_created,
            episodes_already_linked=episodes_already_linked,
            episodes_without_task=episodes_without_task,
            errors=errors[:50],
            duration_seconds=duration,
        )

    except Exception as e:
        log.exception("backfill_episode_task_failed", error=str(e))
        return EpisodeRelationshipBackfillResult(
            success=False,
            relationships_created=relationships_created,
            episodes_already_linked=episodes_already_linked,
            episodes_without_task=episodes_without_task,
            errors=[str(e), *errors[:49]],
            duration_seconds=time.time() - start_time,
        )


@dataclass
class SharedProjectBackfillResult:
    """Result of shared project backfill operation."""

    success: bool
    graph_entity_created: bool
    graph_entity_id: str
    entities_updated: int
    entities_already_set: int
    errors: list[str]
    duration_seconds: float


async def backfill_shared_project(
    *,
    organization_id: str,
    shared_project_graph_id: str,
    dry_run: bool = False,
) -> SharedProjectBackfillResult:
    """Create shared project graph entity and reassign orphan entities.

    This is part of the shared project migration. It:
    1. Creates the graph entity for the shared project if it doesn't exist
    2. Updates all Episodic/Entity nodes with NULL project_id to use the shared project

    Args:
        organization_id: Organization UUID.
        shared_project_graph_id: The graph ID for the shared project (from Postgres).
        dry_run: If True, only report what would be done.

    Returns:
        SharedProjectBackfillResult with statistics.
    """
    from sibyl_core.models.projects import SHARED_PROJECT_DESCRIPTION, SHARED_PROJECT_NAME

    log.info(
        "backfill_shared_project_start",
        organization_id=organization_id,
        shared_project_id=shared_project_graph_id,
        dry_run=dry_run,
    )
    start_time = time.time()

    errors: list[str] = []
    graph_entity_created = False
    entities_updated = 0
    entities_already_set = 0

    try:
        runtime = await get_legacy_graph_runtime(organization_id)
        entity_manager = runtime.entity_manager
        relationship_manager = runtime.relationship_manager

        # Step 1: Create or get the shared project graph entity
        import contextlib

        from sibyl_core.errors import EntityNotFoundError

        existing_project = None
        with contextlib.suppress(EntityNotFoundError):
            existing_project = await entity_manager.get(shared_project_graph_id)

        if existing_project:
            log.info(
                "shared_project_entity_exists",
                id=shared_project_graph_id,
            )
        else:
            if dry_run:
                log.info(
                    "would_create_shared_project_entity",
                    id=shared_project_graph_id,
                )
                graph_entity_created = True
            else:
                # Create the shared project entity
                project_entity = Entity(
                    id=shared_project_graph_id,
                    name=SHARED_PROJECT_NAME,
                    entity_type=EntityType.PROJECT,
                    description=SHARED_PROJECT_DESCRIPTION,
                    content=SHARED_PROJECT_DESCRIPTION,
                    metadata={
                        "is_shared": True,
                        "organization_id": organization_id,
                    },
                )
                await entity_manager.create_direct(project_entity)
                graph_entity_created = True
                log.info(
                    "shared_project_entity_created",
                    id=shared_project_graph_id,
                )

        orphan_entities: list[tuple[str, str]] = []
        offset = 0
        while True:
            entities = await entity_manager.list_all(
                limit=BACKFILL_PAGE_SIZE,
                offset=offset,
                include_archived=True,
            )
            if not entities:
                break

            offset += len(entities)
            for entity in entities:
                entity_type = getattr(entity, "entity_type", None)
                if entity_type == EntityType.PROJECT:
                    continue

                metadata = getattr(entity, "metadata", None)
                metadata = metadata if isinstance(metadata, dict) else {}
                project_id = getattr(entity, "project_id", None) or metadata.get("project_id")

                if project_id:
                    entities_already_set += 1
                    continue

                entity_id = getattr(entity, "id", None)
                if entity_id:
                    type_value = (
                        entity_type.value
                        if hasattr(entity_type, "value")
                        else str(entity_type or "")
                    )
                    orphan_entities.append((str(entity_id), type_value))

        log.info("orphan_entities_found", count=len(orphan_entities))

        # Step 3: Update orphan entities to use shared project
        for entity_id, entity_type in orphan_entities:

            if dry_run:
                log.debug(
                    "would_set_project_id",
                    entity_id=entity_id,
                    entity_type=entity_type,
                    project_id=shared_project_graph_id,
                )
                entities_updated += 1
            else:
                try:
                    await entity_manager.update(
                        entity_id,
                        {"project_id": shared_project_graph_id},
                    )

                    # Also create BELONGS_TO relationship if entity type warrants it
                    if entity_type in {"task", "epic", "milestone"}:
                        await relationship_manager.create(
                            Relationship(
                                id=f"rel_{uuid4().hex}",
                                relationship_type=RelationshipType.BELONGS_TO,
                                source_id=entity_id,
                                target_id=shared_project_graph_id,
                            )
                        )

                    entities_updated += 1
                    log.debug(
                        "entity_project_id_set",
                        entity_id=entity_id,
                        project_id=shared_project_graph_id,
                    )

                except Exception as e:
                    errors.append(f"Failed to update {entity_id}: {e}")
                    log.warning(
                        "entity_update_failed",
                        entity_id=entity_id,
                        error=str(e),
                    )

        if not dry_run:
            entities_already_set += entities_updated

        duration = time.time() - start_time
        log.info(
            "backfill_shared_project_complete",
            graph_entity_created=graph_entity_created,
            entities_updated=entities_updated,
            entities_already_set=entities_already_set,
            errors=len(errors),
            duration=duration,
            dry_run=dry_run,
        )

        return SharedProjectBackfillResult(
            success=True,
            graph_entity_created=graph_entity_created,
            graph_entity_id=shared_project_graph_id,
            entities_updated=entities_updated,
            entities_already_set=entities_already_set,
            errors=errors[:50],
            duration_seconds=duration,
        )

    except Exception as e:
        log.exception("backfill_shared_project_failed", error=str(e))
        return SharedProjectBackfillResult(
            success=False,
            graph_entity_created=graph_entity_created,
            graph_entity_id=shared_project_graph_id,
            entities_updated=entities_updated,
            entities_already_set=entities_already_set,
            errors=[str(e), *errors[:49]],
            duration_seconds=time.time() - start_time,
        )
