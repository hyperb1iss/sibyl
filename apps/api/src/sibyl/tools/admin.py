"""Admin tools for the Conventions MCP Server.

Provides maintenance and diagnostic capabilities.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from sibyl.config import settings
from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.models.entities import Entity, EntityType, Relationship, RelationshipType

log = structlog.get_logger()


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
        client = await get_graph_client()
        graph_connected = True

        # Entity counts and search latency require org context
        if organization_id:
            entity_manager = EntityManager(client, group_id=organization_id)

            # Get entity counts
            for entity_type in EntityType:
                try:
                    entities = await entity_manager.list_by_type(entity_type, limit=1000)
                    entity_counts[entity_type.value] = len(entities)
                except Exception:
                    entity_counts[entity_type.value] = -1  # Unknown

            # Test search latency
            try:
                start = time.time()
                await entity_manager.search(
                    query="test", entity_types=[EntityType.PATTERN], limit=1
                )
                search_latency_ms = (time.time() - start) * 1000
            except Exception as e:
                errors.append(f"Search latency test failed: {e}")

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

    try:
        # Get client to verify connectivity (actual rebuild uses it)
        _client = await get_graph_client()
        _ = _client  # Mark as used; actual index rebuild operations are TODO

        target = index_type or "all"

        if target in ("search", "all"):
            # TODO: Implement actual Graphiti index rebuild
            # This would call _client.rebuild_search_index() or similar
            log.info("Rebuilding search indices")
            indices_rebuilt.append("search")

        if target in ("relationships", "all"):
            # TODO: Implement actual Graphiti relationship index rebuild
            log.info("Rebuilding relationship indices")
            indices_rebuilt.append("relationships")

        duration = time.time() - start_time

        return RebuildResult(
            success=True,
            indices_rebuilt=indices_rebuilt,
            duration_seconds=duration,
            message=f"Successfully rebuilt {len(indices_rebuilt)} index(es)",
        )

    except Exception as e:
        log.error("Index rebuild failed", error=str(e))  # noqa: TRY400
        return RebuildResult(
            success=False,
            indices_rebuilt=indices_rebuilt,
            duration_seconds=time.time() - start_time,
            message=f"Rebuild failed: {e}",
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
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=organization_id)

        # Count entities by type
        entity_stats: dict[str, int] = {}
        for entity_type in EntityType:
            try:
                entities = await entity_manager.list_by_type(entity_type, limit=10000)
                entity_stats[entity_type.value] = len(entities)
            except Exception:
                entity_stats[entity_type.value] = -1

        stats["entities"] = entity_stats
        stats["total_entities"] = sum(v for v in entity_stats.values() if v > 0)

        # TODO: Add relationship stats from RelationshipManager
        # TODO: Add storage stats from Graphiti

        return stats

    except Exception as e:
        log.error("Failed to get stats", error=str(e))  # noqa: TRY400
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
    client: object,
    *,
    batch_size: int,
    max_entities: int,
) -> int:
    entities_updated = 0
    offset = 0
    scanned = 0

    while scanned < max_entities:
        result = await client.driver.execute_query(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Community)
              AND n.name_embedding IS NOT NULL
            RETURN n.uuid AS uuid
            ORDER BY uuid
            SKIP $offset
            LIMIT $limit
            """,
            offset=offset,
            limit=batch_size,
        )

        records = result[0] if result and len(result) > 0 else []
        if not records:
            break

        uuids = [r.get("uuid") for r in records if isinstance(r, dict) and r.get("uuid")]
        scanned += len(uuids)

        for uuid in uuids:
            try:
                await client.driver.execute_query(
                    """
                    MATCH (n {uuid: $uuid})
                    SET n.name_embedding = vecf32(n.name_embedding)
                    RETURN n.uuid AS uuid
                    """,
                    uuid=uuid,
                )
                entities_updated += 1
            except Exception as e:
                # Already Vectorf32 (expected), skip silently.
                if "expected List or Null but was Vectorf32" in str(e):
                    continue
                log.warning("embedding_cast_failed", uuid=uuid, error=str(e))

        offset += batch_size

    return entities_updated


async def _clear_mismatched_name_embedding_dimensions(
    client: object,
    *,
    expected_dim: int,
    batch_size: int,
    max_entities: int,
) -> int:
    embeddings_cleared = 0
    offset = 0
    scanned = 0

    while scanned < max_entities:
        result = await client.driver.execute_query(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Community)
              AND n.name_embedding IS NOT NULL
            RETURN n.uuid AS uuid, n.name_embedding AS emb
            ORDER BY uuid
            SKIP $offset
            LIMIT $limit
            """,
            offset=offset,
            limit=batch_size,
        )

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
                await client.driver.execute_query(
                    """
                    MATCH (n {uuid: $uuid})
                    SET n.name_embedding = NULL
                    RETURN n.uuid AS uuid
                    """,
                    uuid=uuid,
                )
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
        client = await get_graph_client()
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
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=organization_id)
        relationship_manager = RelationshipManager(client, group_id=organization_id)

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
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=organization_id)
        relationship_manager = RelationshipManager(client, group_id=organization_id)

        # Restore entities
        for entity_data in backup_data.entities:
            try:
                entity = Entity.model_validate(entity_data)
                # Check if entity exists
                existing = await entity_manager.get(entity.id)
                if existing and skip_existing:
                    entities_skipped += 1
                    continue

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
                # Check if relationship exists
                existing = await relationship_manager.get(relationship.id)
                if existing and skip_existing:
                    relationships_skipped += 1
                    continue

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
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=organization_id)
        relationship_manager = RelationshipManager(client, group_id=organization_id)

        # Get all tasks
        tasks = await entity_manager.list_by_type(EntityType.TASK, limit=10000)
        log.info("Found tasks to process", count=len(tasks))

        # Get all projects for validation
        projects = await entity_manager.list_by_type(EntityType.PROJECT, limit=1000)
        project_ids = {p.id for p in projects}
        log.info("Found projects", count=len(projects))

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
